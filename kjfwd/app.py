from __future__ import annotations

import argparse
import logging
from pathlib import Path

from kjfwd_bot.service import run


HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="柯基服务队微信群答疑机器人")
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--env", type=Path, default=HERE.parent / ".env")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    run(args.config, args.env)


if __name__ == "__main__":
    main()
