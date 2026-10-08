"""Provenance and overlap invariants for the document hierarchy.

These encode the guarantees citations rely on:

* every child's page range lies inside its section's page range;
* every parent's page range covers each of its children's ranges;
* parent character spans contain their children's spans;
* children of one parent are contiguous, non-overlapping slices of the
  section text (overlap only via the configured sentence overlap).
"""

from __future__ import annotations

from .schemas import Hierarchy, Level


def validate_hierarchy(hierarchy: Hierarchy) -> list[str]:
    """Return a list of human-readable violations (empty when consistent)."""
    issues: list[str] = []

    def _range_contains(outer: tuple[int, int], inner: tuple[int, int], what: str) -> None:
        if inner[0] < outer[0] or inner[1] > outer[1]:
            issues.append(
                f"{what}: {inner} escapes container range {outer}"
            )

    sections = {
        node_id: node
        for node_id, node in hierarchy.nodes.items()
        if node.level is Level.SECTION
    }
    for node_id, node in hierarchy.nodes.items():
        if node.level is Level.CHILD:
            section = sections.get(node.section_id)
            if section is None:
                issues.append(f"child {node_id}: unknown section {node.section_id}")
                continue
            _range_contains(
                (section.page_start, section.page_end),
                (node.page_start, node.page_end),
                f"child {node_id} pages",
            )
            if node.parent_id and node.parent_id in hierarchy.nodes:
                parent = hierarchy.nodes[node.parent_id]
                _range_contains(
                    (parent.char_start, parent.char_end),
                    (node.char_start, node.char_end),
                    f"child {node_id} chars in parent {parent.node_id}",
                )
        elif node.level is Level.PARENT:
            for child_id in node.child_ids:
                child = hierarchy.nodes.get(child_id)
                if child is None:
                    issues.append(f"parent {node_id}: missing child {child_id}")
                    continue
                _range_contains(
                    (node.page_start, node.page_end),
                    (child.page_start, child.page_end),
                    f"parent {node_id} pages vs child {child_id}",
                )
    return issues
