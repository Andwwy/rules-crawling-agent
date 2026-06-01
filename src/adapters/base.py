"""Shared types for the adapter pattern (see DESIGN.md §2.5).

An adapter is responsible for discovery + fetching + snapshotting for one
source kind. It emits FetchedFile triples; everything downstream
(extractor, embedder, judge, DB writer) is source-agnostic.
"""

from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass
class FetchedFile:
    project_host: str           # 'github.com'
    project_owner: str          # 'PatrickJS'
    project_name: str           # 'awesome-cursorrules'
    project_canonical_url: str  # 'https://github.com/PatrickJS/awesome-cursorrules'
    file_path: str              # '.cursor/rules/python.mdc'
    source_kind: str            # one of rules_file_kind enum values (maps to rules_file.kind)
    raw_content: str
    commit_sha: str | None = None
    snapshot_url: str | None = None


class Adapter(Protocol):
    """Implementations: yield one FetchedFile per discovered source file."""
    def discover(self) -> Iterator[FetchedFile]: ...
