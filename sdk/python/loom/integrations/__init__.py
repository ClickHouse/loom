"""Adapters that present loom's memory and documents through agent frameworks'
own interfaces. The LangChain retrievers need the framework: install the SDK's
extra for it (``pip install 'clickhouse-loom[langchain]'``). The LangGraph and
Strands adapters import without their framework, through structural twins of
its types."""
