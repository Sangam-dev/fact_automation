from desktop.state.recognizer import Observation, WindowView


def test_foreground_window_is_considered_before_pinned_siblings():
    main_menu = WindowView(
        title="FACT ERP.NG",
        z_order=0,
        is_foreground=False,
    )
    add_form = WindowView(
        title="FACT ERP.NG - Sale Order - Add",
        z_order=1,
        is_foreground=True,
    )

    ordered = Observation(
        process="FACTERPNG.exe",
        windows=(main_menu, add_form),
    ).deciding_windows()

    assert ordered[0] is add_form
    assert ordered[1] is main_menu
