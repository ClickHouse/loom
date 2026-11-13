"""Adapters that present loom's memory and documents through agent frameworks'
own interfaces. The LangChain and LlamaIndex adapters need their framework:
install the SDK's extra for it (``pip install 'clickhouse-loom[langchain]'``,
``[llamaindex]``). The LangGraph and Strands adapters import without their
framework, through structural twins of its types."""
