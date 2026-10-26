"""Install independent user services for a prepared trial. Does not fund or start it."""
from __future__ import annotations
import argparse
from pathlib import Path
import subprocess


def install(repo: Path, environment: Path, units: Path) -> None:
    units.mkdir(parents=True,exist_ok=True)
    common=f'EnvironmentFile={environment}\nWorkingDirectory={repo}\nUMask=0077\n'
    shared=repo/'packages/czquant/.venv/bin/czquant'
    services={
        'market':f'{shared} monitor market',
        'account':f'{shared} monitor account',
        'sum-low':f'{repo}/strategies/kalshi_sum_check/.venv/bin/python {repo}/strategies/kalshi_sum_check/paper_ws.py --feed rest --preset low',
        'sum-high':f'{repo}/strategies/kalshi_sum_check/.venv/bin/python {repo}/strategies/kalshi_sum_check/paper_ws.py --feed rest --preset high',
    }
    for name,command in services.items():
        extra=f'Environment=CZQUANT_PORTFOLIO={name}\n' if name.startswith('sum-') else ''
        (units/f'czq-{name}.service').write_text(f'[Unit]\nDescription=cz-quant {name}\n[Service]\n{common}{extra}ExecStart={command}\nRestart=on-failure\nRestartSec=15\n[Install]\nWantedBy=default.target\n')
    for name,strategy in [('election-independent','2026-midterm-prediction-arbitrage'),('election-dependence','2026-midterm-dependence-arbitrage')]:
        directory=repo/'strategies'/strategy
        (units/f'czq-{name}.service').write_text(f'[Unit]\nDescription=cz-quant {name}\n[Service]\nType=oneshot\nEnvironmentFile={environment}\nWorkingDirectory={directory}\nUMask=0077\nEnvironment=CZQUANT_PORTFOLIO={name}\nExecStart={directory}/.venv/bin/strategy --seed 20261024\nTimeoutStartSec=900\n')
        (units/f'czq-{name}.timer').write_text(f'[Unit]\nDescription=cz-quant {name} cadence\n[Timer]\nOnActiveSec=1\nOnUnitInactiveSec=15min\nUnit=czq-{name}.service\n[Install]\nWantedBy=timers.target\n')
    for name,script in [('archive','archive.sh'),('sunday','sunday.sh')]:
        (units/f'czq-{name}.service').write_text(f'[Unit]\nDescription=cz-quant {name}\n[Service]\nType=oneshot\n{common}ExecStart={repo}/scripts/experiments/{script}\nTimeoutStartSec=1800\n')
    (units/'czq-archive.timer').write_text('[Unit]\nDescription=cz-quant hourly private backup\n[Timer]\nOnActiveSec=5min\nOnUnitActiveSec=1h\n[Install]\nWantedBy=timers.target\n')
    (units/'czq-sunday.timer').write_text('[Unit]\nDescription=cz-quant Sunday comparison\n[Timer]\nOnCalendar=2026-10-25 18:05:00 UTC\nPersistent=true\nAccuracySec=1s\n[Install]\nWantedBy=timers.target\n')
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--environment',type=Path,required=True)
    p.add_argument('--units',type=Path,default=Path.home()/'.config/systemd/user')
    a=p.parse_args()
    install(Path(__file__).resolve().parents[2],a.environment.resolve(),a.units)
