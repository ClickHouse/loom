"""Adapters that present loom's memory and documents through agent frameworks'
own interfaces. An adapter needs its framework, installed with the SDK extra
the module names (``pip install 'clickhouse-loom[langchain]'`` and so on);
the LangGraph and Strands adapters import without theirs, through structural
twins of its types."""
