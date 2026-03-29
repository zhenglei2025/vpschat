from jlpt_local_materials import build_intermediate_static_cache


def main() -> None:
    payload = build_intermediate_static_cache(force_rebuild=True)
    upper_count = len(payload.get("upper", {}))
    lower_count = len(payload.get("lower", {}))
    print(f"built intermediate OCR cache: upper={upper_count}, lower={lower_count}")


if __name__ == "__main__":
    main()