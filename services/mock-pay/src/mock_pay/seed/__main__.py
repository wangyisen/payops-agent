"""Regenerate the seed data: ``python -m mock_pay.seed --data-dir ... --labels ...``."""

import argparse
from pathlib import Path

from mock_pay.seed.build import build, render


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="where the API data goes")
    parser.add_argument("--labels", type=Path, required=True, help="scenario labels JSON file")
    args = parser.parse_args()

    files, labels = render(*build())
    args.data_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (args.data_dir / name).write_text(content, encoding="utf-8")
    args.labels.parent.mkdir(parents=True, exist_ok=True)
    args.labels.write_text(labels, encoding="utf-8")
    print(f"wrote {len(files)} files to {args.data_dir} and labels to {args.labels}")


if __name__ == "__main__":
    main()
