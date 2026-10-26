"""Factor model: races move together through a shared latent swing.

Each race i gets a latent Democratic margin

    M_i = sqrt(1 + tau^2) * x_i  +  tau * U  +  e_i,      D wins iff M_i > 0,

where x_i = probit(p_i) is set from the race's market probability p_i, U ~ N(0, 1)
is the chamber's swing, tau its standard deviation, and e_i ~ N(0, 1) is
race-specific noise. The sqrt(1 + tau^2) factor is what keeps every race's win
probability at exactly p_i for any tau: the swing changes only how races move
*together*, never the price of any single race. tau = 0 is the independent model
(``simulation.py``). Two races share latent correlation tau^2 / (1 + tau^2).

The House has 368 seats Kalshi does not price. Holding them fixed at their 2024
result caps the model near 250 Democratic seats, but the seat-count market puts
~15% on 250 or more. So they get latent margins too: each unpriced Republican
seat has a small baseline chance ``flip_rep`` of going Democratic, and each
unpriced Democratic seat a chance ``flip_dem`` of going Republican. The swing
moves them like any other race, so a wave can reach into seats nobody quotes.
Both chances are calibrated to the seat-count market (``calibration.py``); they
are part of the baseline, not of the dependence.

Factors
-------
One factor: the House and Senate share the same swing (U_H = U_S = Z) with one
tau. Two factors: each chamber has its own tau. The two swings share a national
component; the chamber with the larger tau gets an extra chamber-only factor for
the excess:

    tau_small * U_small = c * Z,   tau_large * U_large = c * Z + a * W,
    c = min(tau_H, tau_S),  a = sqrt(tau_large^2 - c^2).

This is the smallest departure from one factor that lets the chambers' swings
differ in size. The split between national and chamber-only swing is not
identified by single-chamber markets; taking as much as possible as national is
the assumption, and it is the one the combo market then tests.

Computation
-----------
Given the swing, races are independent, so the seat distribution is exact: a
Poisson-binomial over the priced races, convolved with two binomials for the
unpriced seats. The swing is integrated out with Gauss-Hermite quadrature, which
makes the output exact and smooth in the parameters (the calibration needs
that). ``simulate_factor`` runs the same model by Monte Carlo; it exists to
check the quadrature and to verify that race marginals are preserved.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.polynomial.hermite_e import hermegauss
from scipy.special import gammaln, ndtr, ndtri, xlog1py, xlogy

from strategy.constants import (
    HOUSE_MAJORITY,
    HOUSE_TOTAL,
    SENATE_DEM_CONTROL,
    SENATE_NOT_UP_DEM,
    SENATE_TOTAL,
)

NODES = 64
OUTCOMES = ("DD", "DR", "RD", "RR")


@dataclass(frozen=True)
class FactorParams:
    sigma_house: float
    sigma_senate: float
    flip_rep: float = 0.0  # P(an unpriced Republican House seat goes D)
    flip_dem: float = 0.0  # P(an unpriced Democratic House seat goes R)

    @classmethod
    def one_factor(cls, sigma: float, flip_rep: float = 0.0, flip_dem: float = 0.0) -> "FactorParams":
        return cls(sigma, sigma, flip_rep, flip_dem)

    @property
    def factors(self) -> int:
        return 1 if self.sigma_house == self.sigma_senate else 2

    @property
    def common(self) -> float:
        """Standard deviation of the national swing shared by both chambers."""
        return min(self.sigma_house, self.sigma_senate)

    @property
    def cross_correlation(self) -> float:
        """Correlation between the House and Senate swings."""
        if self.sigma_house == 0 or self.sigma_senate == 0:
            return 1.0 if self.sigma_house == self.sigma_senate else 0.0
        return self.common**2 / (self.sigma_house * self.sigma_senate)


def latent_correlation(sigma: float) -> float:
    """Correlation of two races' latent margins in a chamber with swing ``sigma``."""
    return sigma**2 / (1.0 + sigma**2)


@dataclass(frozen=True)
class Chamber:
    """A chamber's races, reduced to what the seat distribution needs."""

    name: str
    race_ids: tuple[str, ...]  # races with 0 < p < 1, sorted
    x: np.ndarray  # their probits
    fixed_dem: int  # Democratic seats that cannot change (Senate seats not up)
    unpriced_rep: int  # unpriced Republican seats that may flip
    unpriced_dem: int  # unpriced Democratic seats that may flip
    threshold: int  # Democratic seats needed for control
    size: int


def chambers(probs: dict[str, float]) -> tuple[Chamber, Chamber]:
    """Split a ``{race_id: p}`` dict into the House and the Senate."""

    def split(prefix: str):
        ids = sorted(k for k in probs if k.startswith(prefix))
        uncertain = tuple(k for k in ids if 0.0 < probs[k] < 1.0)
        x = ndtri(np.array([probs[k] for k in uncertain], dtype=np.float64))
        sure_d = sum(1 for k in ids if probs[k] >= 1.0)
        sure_r = sum(1 for k in ids if probs[k] <= 0.0)
        return uncertain, x, sure_d, sure_r

    h_ids, h_x, h_d, h_r = split("house:")
    s_ids, s_x, s_d, _ = split("senate:")
    house = Chamber("House", h_ids, h_x, 0, h_r, h_d, HOUSE_MAJORITY, HOUSE_TOTAL)
    senate = Chamber(
        "Senate", s_ids, s_x, SENATE_NOT_UP_DEM + s_d, 0, 0, SENATE_DEM_CONTROL, SENATE_TOTAL
    )
    return house, senate


def gauss_hermite(nodes: int = NODES) -> tuple[np.ndarray, np.ndarray]:
    """Nodes and weights integrating against the standard normal density."""
    z, w = hermegauss(nodes)
    return z, w / math.sqrt(2.0 * math.pi)


# --- Conditional seat distributions ------------------------------------------


def priced_pmf(ch: Chamber, tau: float, u: np.ndarray) -> np.ndarray:
    """Poisson-binomial pmf of priced-race wins given swings ``u``: (len(u), n+1)."""
    q = ndtr(math.sqrt(1.0 + tau**2) * ch.x[None, :] + tau * u[:, None])
    pmf = np.zeros((len(u), q.shape[1] + 1))
    pmf[:, 0] = 1.0
    for j in range(q.shape[1]):
        p = q[:, j : j + 1]
        pmf[:, 1 : j + 2] = pmf[:, 1 : j + 2] * (1.0 - p) + pmf[:, 0 : j + 1] * p
        pmf[:, 0] *= 1.0 - p[:, 0]
    return pmf


def seat_pmf(
    ch: Chamber,
    tau: float,
    flip_rep: float,
    flip_dem: float,
    u: np.ndarray,
    priced: np.ndarray | None = None,
) -> np.ndarray:
    """Pmf of the chamber's Democratic seat total given swings ``u``.

    Shape ``(len(u), size + 1)``. ``priced`` may pass a precomputed
    ``priced_pmf(ch, tau, u)`` so the calibration can vary the flip rates
    without redoing the Poisson-binomial.
    """
    pmf = priced_pmf(ch, tau, u) if priced is None else priced
    scale = math.sqrt(1.0 + tau**2)
    offset = ch.fixed_dem

    if ch.unpriced_rep:
        if flip_rep > 0.0:
            q = ndtr(scale * ndtri(flip_rep) + tau * u)
            pmf = _convolve(pmf, _binomial_pmf(ch.unpriced_rep, q))
    if ch.unpriced_dem:
        if flip_dem > 0.0:
            hold = ndtr(scale * ndtri(1.0 - flip_dem) + tau * u)
            pmf = _convolve(pmf, _binomial_pmf(ch.unpriced_dem, hold))
        else:
            offset += ch.unpriced_dem

    out = np.zeros((len(u), ch.size + 1))
    width = min(pmf.shape[1], ch.size + 1 - offset)
    out[:, offset : offset + width] = pmf[:, :width]
    return out


def _binomial_pmf(n: int, q: np.ndarray) -> np.ndarray:
    """Binomial(n, q) pmf for each q, shape (len(q), n + 1), stable at q = 0 or 1."""
    k = np.arange(n + 1)[None, :]
    q = q[:, None]
    log_pmf = gammaln(n + 1) - gammaln(k + 1) - gammaln(n - k + 1) + xlogy(k, q) + xlog1py(n - k, -q)
    return np.exp(log_pmf)


def _convolve(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Row-wise convolution. Direct, not FFT: with non-negative inputs it keeps
    full relative precision in the far tails, which the calibration's gradients
    depend on (an FFT leaves ~1e-17 absolute noise there)."""
    out = np.empty((a.shape[0], a.shape[1] + b.shape[1] - 1))
    for i in range(a.shape[0]):
        out[i] = np.convolve(a[i], b[i])
    return out


def chamber_distribution(
    ch: Chamber, tau: float, flip_rep: float = 0.0, flip_dem: float = 0.0, nodes: int = NODES
) -> np.ndarray:
    """Unconditional pmf of the chamber's Democratic seats."""
    u, w = gauss_hermite(nodes)
    return w @ seat_pmf(ch, tau, flip_rep, flip_dem, u)


# --- Joint solution ------------------------------------------------------------


@dataclass(frozen=True)
class FactorResult:
    params: FactorParams
    combo: dict[str, float]
    p_house_dem: float
    p_senate_dem: float
    house_pmf: np.ndarray
    senate_pmf: np.ndarray

    @staticmethod
    def _moments(pmf: np.ndarray) -> tuple[float, float]:
        k = np.arange(len(pmf))
        mean = float(k @ pmf)
        return mean, float(math.sqrt(max((k**2) @ pmf - mean**2, 0.0)))

    def house_moments(self) -> tuple[float, float]:
        return self._moments(self.house_pmf)

    def senate_moments(self) -> tuple[float, float]:
        return self._moments(self.senate_pmf)


def solve(probs: dict[str, float], params: FactorParams, nodes: int = NODES) -> FactorResult:
    """Exact outcome distribution of the factor model by quadrature."""
    house, senate = chambers(probs)
    z, wz = gauss_hermite(nodes)
    fr, fd = params.flip_rep, params.flip_dem

    if params.factors == 1:
        tau = params.sigma_house
        h_cond = seat_pmf(house, tau, fr, fd, z)
        s_cond = seat_pmf(senate, tau, 0.0, 0.0, z)
        h = h_cond[:, house.threshold :].sum(axis=1)
        s = s_cond[:, senate.threshold :].sum(axis=1)
        house_pmf, senate_pmf = wz @ h_cond, wz @ s_cond
    else:
        c = params.common
        house_larger = params.sigma_house > params.sigma_senate
        tau_large = max(params.sigma_house, params.sigma_senate)
        a = math.sqrt(tau_large**2 - c**2)
        v, wv = gauss_hermite(nodes)
        # Swings in standard units; the smaller chamber's swing is the national one.
        u_large = ((c * z[:, None] + a * v[None, :]) / tau_large).ravel()
        if house_larger:
            large = seat_pmf(house, params.sigma_house, fr, fd, u_large)
            small = seat_pmf(senate, params.sigma_senate, 0.0, 0.0, z)
            large_ch, small_ch = house, senate
        else:
            large = seat_pmf(senate, params.sigma_senate, 0.0, 0.0, u_large)
            small = seat_pmf(house, params.sigma_house, fr, fd, z)
            large_ch, small_ch = senate, house
        large = large.reshape(len(z), len(v), -1)
        large_ctrl = large[:, :, large_ch.threshold :].sum(axis=2) @ wv  # per z
        small_ctrl = small[:, small_ch.threshold :].sum(axis=1)
        large_pmf = np.einsum("j,k,jkn->n", wz, wv, large)
        small_pmf = wz @ small
        if house_larger:
            h, s, house_pmf, senate_pmf = large_ctrl, small_ctrl, large_pmf, small_pmf
        else:
            h, s, house_pmf, senate_pmf = small_ctrl, large_ctrl, small_pmf, large_pmf

    combo = {
        "DD": float(wz @ (h * s)),
        "DR": float(wz @ (h * (1.0 - s))),
        "RD": float(wz @ ((1.0 - h) * s)),
        "RR": float(wz @ ((1.0 - h) * (1.0 - s))),
    }
    return FactorResult(
        params=params,
        combo=combo,
        p_house_dem=float(wz @ h),
        p_senate_dem=float(wz @ s),
        house_pmf=house_pmf,
        senate_pmf=senate_pmf,
    )


# --- Monte Carlo check -----------------------------------------------------------


@dataclass(frozen=True)
class FactorSimulation:
    combo: dict[str, float]
    p_house_dem: float
    p_senate_dem: float
    race_win_rates: dict[str, float]  # simulated P(D wins) for each priced race
    n: int


def simulate_factor(
    probs: dict[str, float], params: FactorParams, n: int = 100_000, seed: int | None = None
) -> FactorSimulation:
    """Monte Carlo of the same model. Used to verify ``solve`` and the marginals."""
    house, senate = chambers(probs)
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n)
    if params.factors == 1:
        u_house = u_senate = z
    else:
        c = params.common
        w = rng.standard_normal(n)
        if params.sigma_house > params.sigma_senate:
            a = math.sqrt(params.sigma_house**2 - c**2)
            u_house, u_senate = (c * z + a * w) / params.sigma_house, z
        else:
            a = math.sqrt(params.sigma_senate**2 - c**2)
            u_house, u_senate = z, (c * z + a * w) / params.sigma_senate

    rates: dict[str, float] = {}
    house_seats = _simulate_chamber(house, params.sigma_house, params.flip_rep, params.flip_dem, u_house, rng, rates)
    senate_seats = _simulate_chamber(senate, params.sigma_senate, 0.0, 0.0, u_senate, rng, rates)
    h = house_seats >= house.threshold
    s = senate_seats >= senate.threshold
    combo = {
        "DD": float(np.mean(h & s)),
        "DR": float(np.mean(h & ~s)),
        "RD": float(np.mean(~h & s)),
        "RR": float(np.mean(~h & ~s)),
    }
    return FactorSimulation(combo, float(h.mean()), float(s.mean()), rates, n)


def _simulate_chamber(ch, tau, flip_rep, flip_dem, u, rng, rates) -> np.ndarray:
    scale = math.sqrt(1.0 + tau**2)
    noise = rng.standard_normal((len(u), len(ch.x)))
    wins = scale * ch.x[None, :] + tau * u[:, None] + noise > 0.0
    for rid, rate in zip(ch.race_ids, wins.mean(axis=0)):
        rates[rid] = float(rate)
    seats = ch.fixed_dem + wins.sum(axis=1)
    if ch.unpriced_rep:
        q = ndtr(scale * ndtri(flip_rep) + tau * u) if flip_rep > 0 else np.zeros(len(u))
        seats = seats + rng.binomial(ch.unpriced_rep, q)
    if ch.unpriced_dem:
        hold = ndtr(scale * ndtri(1.0 - flip_dem) + tau * u) if flip_dem > 0 else np.ones(len(u))
        seats = seats + rng.binomial(ch.unpriced_dem, hold)
    return seats
