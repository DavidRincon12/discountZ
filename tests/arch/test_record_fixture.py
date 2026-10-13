from discountz.__main__ import build_parser, main


def test_record_fixture_arguments() -> None:
    args = build_parser().parse_args(
        ["record-fixture", "exito", "busqueda_ssd", "https://www.exito.com/api/x?ft=ssd"]
    )
    assert (args.source, args.name, args.url, args.folder) == (
        "exito",
        "busqueda_ssd",
        "https://www.exito.com/api/x?ft=ssd",
        None,
    )


def test_record_fixture_unknown_source_is_config_error() -> None:
    assert main(["record-fixture", "nada", "x", "https://example.com"]) == 2
