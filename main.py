import sys


def main() -> None:
    if sys.argv[1:]:
        raise SystemExit(f"Unknown arguments: {' '.join(sys.argv[1:])}")

    from automation.run_classical_bot_service import main as service_main

    service_main()


if __name__ == "__main__":
    main()
