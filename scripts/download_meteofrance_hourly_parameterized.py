from pathlib import Path

from weather_analysis.cli import run


DEPARTMENTS: set[str] | None = {"44"}
START_YEAR: int | None = 2010
END_YEAR: int | None = 2025
STATION_CATEGORY = "main"
LIST_ONLY = True
OUTPUT_ROOT = Path("data/meteo_france")


def main() -> None:
    action = "status" if LIST_ONLY else "download"
    arguments = ["meteo-france", action, "--category", STATION_CATEGORY]
    for department in sorted(DEPARTMENTS or set()):
        arguments.extend(("--department", department))
    if START_YEAR is not None:
        arguments.extend(("--start-year", str(START_YEAR)))
    if END_YEAR is not None:
        arguments.extend(("--end-year", str(END_YEAR)))
    directory_name = "hourly_raw" if STATION_CATEGORY == "main" else "hourly_complementary"
    arguments.extend(("--output-dir", str(OUTPUT_ROOT / directory_name)))
    print("Compatibility wrapper: prefer the `weather-analysis meteo-france` CLI.")
    raise SystemExit(run(arguments))


if __name__ == "__main__":
    main()
