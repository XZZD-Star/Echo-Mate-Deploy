"""Deterministic text-routing policy for Feishu page contexts."""


PC_PAGES = frozenset({
    "pc",
    "pc_single",
    "pc_fixed",
    "pc_fixed_new",
    "pc_history",
    "pc_history_detail",
})
PC_SINGLE_CAPTURE_PAGES = frozenset({
    "pc_single",
    "pc_fixed_new",
})


def route_mode(page):
    """Return echo, pc_control, pc_capture, or pc_fixed_capture."""
    if page == "pc_fixed":
        return "pc_fixed_capture"
    if page in PC_SINGLE_CAPTURE_PAGES:
        return "pc_capture"
    if page in PC_PAGES:
        return "pc_control"
    return "echo"


def is_pc_page(page):
    return page in PC_PAGES
