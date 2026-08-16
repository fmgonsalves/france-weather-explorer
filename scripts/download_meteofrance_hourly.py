from weather_analysis.cli import run


def main() -> None:
    print("Compatibility wrapper: prefer `france-weather-explorer meteo-france download`.")
    raise SystemExit(run(["meteo-france", "download"]))


if __name__ == "__main__":
    main()
