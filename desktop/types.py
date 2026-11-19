"""Screen, window, and UIA candidate models (Segment 2)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    """A rectangle in physical screen pixels."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersects(self, other: "Rect") -> bool:
        return not (
            self.right <= other.left
            or self.left >= other.right
            or self.bottom <= other.top
            or self.top >= other.bottom
        )


@dataclass(frozen=True)
class WindowInfo:
    """A top-level window snapshot."""

    handle: int
    title: str
    process_name: str
    class_name: str
    rect: Rect
    client_rect: Rect
    dpi: int

    @property
    def dpi_scale(self) -> float:
        return self.dpi / 96.0


#: Control types that can be clicked.
#:
#: ``Button`` alone is not enough: DevExpress, Qt and other custom-painted
#: toolkits expose real buttons as ``Pane``/``Custom``/``Group``. One set is
#: shared by the inspector, the planner and the executor so the three cannot
#: drift apart and silently stop seeing a class of buttons.
CLICKABLE_CONTROL_TYPES = frozenset(
    {
        "Button",
        "Hyperlink",
        "SplitButton",
        "Image",
        "MenuItem",
        "TabItem",
        "ListItem",
        "TreeItem",
        "Pane",
        "Custom",
        "Group",
    }
)

#: Container types that are only worth listing when they look like a button.
CONTAINER_CONTROL_TYPES = frozenset({"Pane", "Custom", "Group"})

#: Control types that accept typed text.
EDITABLE_CONTROL_TYPES = frozenset({"Edit", "Document", "Spinner"})

#: Names a toolkit gives a picture-only button; the automation id is the label.
_GENERIC_NAMES = frozenset(
    {
        "system.drawing.bitmap",
        "system.drawing.icon",
        "bitmap",
        "image",
        "picture",
        "pictureedit",
    }
)


@dataclass(frozen=True)
class UIACandidate:
    """One UI Automation control found inside the target window."""

    candidate_id: str
    name: str
    control_type: str
    automation_id: str
    class_name: str
    rect: Rect
    value: str
    enabled: bool

    @property
    def label(self) -> str:
        """The name to show and match: the caption, or the id for image buttons.

        A ``Pane`` drawn as an image button reports its name as
        ``System.Drawing.Bitmap`` and only its automation id (``btnLogin``)
        identifies it, so the id becomes the target label in that case.
        """
        name = self.name.strip()
        if name and name.casefold() not in _GENERIC_NAMES:
            return name
        return self.automation_id.strip() or name or self.value.strip()
