"""`CodeRepo`: the code plane's repository, composed from one mixin per domain.

Each mixin holds the statements over one table family and shares only what
`CodeRepoBase` provides (the client, `insert_rows`, the index state and the
row mirrors), so no mixin calls into another.
"""

from __future__ import annotations

from server.code.store.index.answers import AnswersMixin
from server.code.store.index.cards import CardsMixin
from server.code.store.index.code_repos import CodeReposMixin
from server.code.store.index.coverage import CoverageMixin
from server.code.store.index.edges import EdgesMixin
from server.code.store.index.embed_queue import EmbedQueueMixin
from server.code.store.index.facets import FacetsMixin
from server.code.store.index.files import FilesMixin
from server.code.store.index.gh_items import GhItemsMixin
from server.code.store.index.graph_stats import GraphStatsMixin
from server.code.store.index.grep import GrepMixin
from server.code.store.index.pr_history import PrHistoryMixin
from server.code.store.index.reviews import ReviewsMixin
from server.code.store.index.scip_staging import ScipStagingMixin
from server.code.store.index.search import SearchMixin
from server.code.store.index.search_memo import SearchMemoMixin
from server.code.store.index.status import StatusMixin
from server.code.store.index.symbols import SymbolsMixin


class CodeRepo(
    FilesMixin,
    EmbedQueueMixin,
    SymbolsMixin,
    EdgesMixin,
    SearchMixin,
    SearchMemoMixin,
    GrepMixin,
    PrHistoryMixin,
    GhItemsMixin,
    ReviewsMixin,
    CardsMixin,
    FacetsMixin,
    GraphStatsMixin,
    ScipStagingMixin,
    StatusMixin,
    AnswersMixin,
    CodeReposMixin,
    CoverageMixin,
):
    """Every loom.code_* read and write: `CodeRepo(ch).method(...)`."""
