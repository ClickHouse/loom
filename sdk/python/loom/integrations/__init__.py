"""Adapters that present loom's memory and documents through agent frameworks'
own interfaces. The LangChain, LlamaIndex and OpenAI Agents adapters need
their framework: install the SDK's extra for it
(``pip install 'clickhouse-loom[langchain]'``, ``[llamaindex]``,
``[openai-agents]``). The LangGraph and Strands adapters import without their
framework, through structural twins of its types."""
