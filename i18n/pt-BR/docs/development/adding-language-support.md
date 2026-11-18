<!-- i18n: source=docs/development/adding-language-support.md blob=ee2dee0c86ab7bfb46f22017d1d71ab8005a2276 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md), which is authoritative.

<a id="add-language-support-to-the-code-index"></a>

# Adicionar suporte a linguagens ao índice de código

O índice de código lê um repositório em duas camadas:

- A **camada rápida** (gramáticas tree-sitter e scanners escritos à mão) é executada em todos os arquivos a cada commit. Ela produz chunks, símbolos e arestas de chamada.
- Uma **camada do compilador** (um indexador SCIP sobre os arquivos alterados) adiciona símbolos e arestas exatos do compilador onde existe um indexador.

Uma linguagem é considerada suportada quando a camada rápida tem uma lane para ela. [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) explica as camadas e lista o que cada linguagem recebe.

<a id="the-one-rule-stay-outside-the-extractor-stamp"></a>

## A regra única: fique fora do extractor stamp

As linhas da camada rápida carregam um carimbo de versão do código que as produziu (`server/code/domain/extract/extractor_stamp.py`):

- O carimbo é um sha256 sobre os **tokens de código** (`logic_tokens`: sem comentários, linhas em branco ou docstrings; um import conta como os nomes que ele vincula, não como o caminho do pacote) de cada módulo em `STAGE_MODULES`, mais as versões instaladas das gramáticas em `STAMPED_GRAMMARS` (`tree-sitter`, `tree-sitter-cpp`, `tree-sitter-python`, `tree-sitter-bash`).
- Quando o carimbo muda, o worker de ingestão rederiva os estágios afetados (`chunk`, `symbol`, `edge`, `guard`) em lotes espaçados, para cada arquivo de cada namespace indexado nas famílias de linguagens afetadas (`_maybe_rechunk` em `server/code/app/index/ingest_pass.py`).
- Um módulo mapeado para uma família em `MODULE_LANGUAGES` (`lang_python.py`, os módulos `ts_*`) afeta apenas os arquivos dessa família. Um módulo compartilhado (`treesitter.py`, `chunker.py`) ou a gramática principal `tree-sitter` afeta todos os arquivos.
- Em uma implantação grande, isso significa horas de ingestão.

Adicione uma nova linguagem onde ela não mova nenhum carimbo:

| Arquivo                                                        | No carimbo? | O que vai lá                                                                                                      |
| -------------------------------------------------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------- |
| `server/code/domain/extract/lang_<lang>.py`                    | não         | A lane: `parse_<lang>(path, source) -> ParsedFile` com símbolos, includes e pontos de chamada.                    |
| `server/code/domain/extract/lang_detect.py`                    | não         | `LANG_BY_EXTENSION` (e as regras de shell): quais arquivos pertencem a essa linguagem.                            |
| `server/code/app/index/ingest_parse.py`                        | não         | Um wrapper `_parse_<lang>` em `_PARSERS`; resolução de includes em `ingest_resolve.py` se a lane tiver imports.   |
| `server/code/domain/extract/lane_units.py`                     | não         | Helpers compartilhados para uma lane **sem** gramática: `span_chunks` e o vinculador de referências `bind_sites`. |
| `server/code/domain/extract/treesitter.py`                     | **sim**     | `lang_for_path` apenas para as linguagens principais carimbadas. Adicionar uma extensão aqui refaz o chunking de todos os namespaces. |
| `chunker.py`, `edges_treesitter.py`, os demais `STAGE_MODULES` | **sim**     | Chunking e extração de arestas compartilhados. Altere-os somente quando a própria extração mudar para todos.      |

As linguagens que `lang_for_path` não nomeia são detectadas em `lang_detect.py`, portanto adicionar uma não move nenhum carimbo. Comentários e docstrings em módulos carimbados podem ser editados livremente. Esses módulos são excluídos da formatação, porque um formatador poderia reescrever as aspas de strings, que são tokens de código.

Todo módulo em `server/code/domain/extract/` está em um estágio de `STAGE_MODULES` ou é nomeado, com um motivo, em `NON_EXTRACTOR_MODULES` em `tests/unit/test_code_extractor_stamp.py`. Coloque um novo módulo de lane em `NON_EXTRACTOR_MODULES`, ou esse teste falha.

<a id="1-choose-the-lanes-shape"></a>

## 1. Escolha o formato da lane

| Formato           | Exemplos                                                                                                   | O que você fornece                                                                                                                                                                          |
| ----------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lane de gramática | `lang_typescript.py`, `lang_rust.py`, `lang_go.py`, `lang_java.py`                                         | Uma dependência de gramática tree-sitter em `pyproject.toml`; símbolos e pontos de chamada extraídos da árvore; os chunks e as arestas vêm do chunker e do edge walker compartilhados.        |
| Lane sem árvore   | `lang_proto.py`, `lang_gotemplate.py`, `lang_hcl.py`, `lang_k8s.py`, `lang_jsonnet.py` (`LANE_UNIT_LANGS`) | Um scanner escrito à mão; `ParsedFile.tree` permanece `None`; chunks de `lane_units.span_chunks`, arestas de `reference_sites` + `SITE_RULES` vinculadas por `lane_units.bind_sites`.         |
| Lane somente de includes | `tla`, `flatbuffers`, `cmake`, `doc` (`_LANE_INCLUDE_LANGS`)                                        | Símbolos e includes resolvidos pelo resolvedor simples de lane.                                                                                                                             |
| Linguagem embutida | SQL dentro de Go e Java (`go_sql_units`, `java_sql_units`), ilhas de shell em Dockerfiles e Makefiles    | Unidades incorporadas ao parse do arquivo hospedeiro (`_join_island_symbols`), geralmente atrás de uma configuração de kill switch.                                                          |
| Somente chunks    | `.css`, arquivos de texto (`lang_text.py`)                                                                 | Apenas a detecção: o arquivo é dividido em chunks e fica pesquisável, sem símbolos.                                                                                                         |

A gramática de uma lane de gramática **não** está em `STAMPED_GRAMMARS`, então atualizá-la não move nenhum stamp. A lane é responsável por rederivar suas próprias linhas.

<a id="2-write-the-parser"></a>

## 2. Escreva o parser

`parse_<lang>(path: str, source: bytes) -> ParsedFile` retorna a tag da linguagem, os símbolos (`treesitter.Symbol`: nome qualificado, tipo, intervalo, assinatura), os includes e o que a etapa de arestas precisa. Siga a lane existente mais próxima:

- **Nomes qualificados** seguem o caminho de módulos da linguagem (Rust: o caminho do crate a partir do caminho do arquivo; `mod.rs` / `lib.rs` dão nome ao seu diretório). O id de um símbolo é o hash do nome qualificado e da assinatura normalizada, portanto um nome instável faz as linhas serem regravadas a cada nova indexação.
- **Includes** são resolvidos contra os caminhos que o namespace contém (`wanted_paths`, depois `resolve_imports` / `resolve_uses` na lane, chamados a partir de `ingest_resolve.py`). Pacotes externos resolvem para nada, não para um palpite.
- **Arestas de chamada** associam primeiro nomes únicos no mesmo arquivo, depois nomes únicos no repositório com confiança menor, e nada quando um nome é ambíguo (`lane_units.bind_sites` faz isso para lanes sem árvore).
- **Tipos de contêiner** (uma classe, um bloco impl) pertencem à lane. Registrar um na tabela por linguagem do chunker altera `chunker.py` e muda o stamp.

Registre a lane:

```python
# server/code/app/index/ingest_parse.py
def _parse_rust(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust(path, source)


_PARSERS = {
    ...
    "rust": _parse_rust,
    ...
}
```

e adicione a extensão a `LANG_BY_EXTENSION` (`".rs": "rust"`). O wrapper importa a lane de forma tardia, então uma gramática opcional continua opcional. Uma nova gramática tree-sitter também entra em `bounded_parse._LOADERS`, por onde toda análise passa primeiro.

<a id="3-test-the-lane"></a>

## 3. Teste a lane

- **Um teste da lane**, `tests/unit/test_code_lang_<lang>.py`, faz o parse de pequenas fontes inline e verifica nomes, tipos, spans, includes e edges. Ignore o teste quando a gramática estiver ausente: `pytest.importorskip("tree_sitter_rust")`. `test_code_lang_rust.py` e `test_code_lang_typescript.py` são os modelos.
- **O corpus das lanes.** Adicione fixtures em `tests/fixtures/code_lanes/<lang>/`: sintéticas, ou trechos de código aberto sob a licença própria, listados em `tests/fixtures/code_lanes/NOTICE.md`. `tests/unit/test_code_lanes_corpus.py` deriva linguagens, símbolos, facetas, includes e edges de cada fixture e os compara com `tests/fixtures/code_lanes/expected.json`. Regenere e revise o diff:

  ```bash
  python -m tests.code_lanes.corpus --write
  git diff tests/fixtures/code_lanes/expected.json
  ```

  Um número que muda ali altera o que cada namespace contém. Revise-o.

- **A proteção de custo.** Um parser adicionado a `ingest_parse._PARSERS` precisa de uma entrada em `SAMPLES` em `tests/unit/lane_walks.py`; então `tests/unit/test_code_regex_cost.py` mede o tempo da lane sobre formas geradas e falha em um percurso que seja mais que linear.
- **O teste de stamp.** `tests/unit/test_code_extractor_stamp.py` com o novo módulo em
  `NON_EXTRACTOR_MODULES`.
- **Recuperação.** Para uma lane que deve responder bem a `code.search` ou `code.symbol`, execute o gate de inteligência de código antes e depois. `make bench-code-intel-dry` verifica o conjunto de perguntas offline; `make bench-code-intel` o executa contra um servidor com um embedder real, nunca o falso ([executando benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="4-bring-existing-namespaces-up-to-date"></a>

## 4. Atualize os namespaces existentes

Uma lane fora do stamp não altera nada que já esteja indexado. Os novos commits usam a lane; os arquivos antigos mantêm suas linhas anteriores (muitas vezes apenas com chunks). Derive-as novamente com um backfill direcionado:

```bash
git -C <checkout of the repository> ls-files '*.rs' > rust-paths.txt
loom code backfill --org <org> --namespace <code-namespace> --paths-file rust-paths.txt
```

`--paths-file` reingere apenas esses caminhos relativos ao repositório, na ponta do branch, e não mexe na marca d'água. Execute o comando onde estão o `.env` do servidor e os espelhos dos repositórios, como no contêiner de workers ([operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

> **Aviso:** se uma mudança precisar tocar um módulo marcado com stamp (uma correção em `chunker.py`, uma extensão em `treesitter.py`), informe isso no PR. Todo deployment que a incorporar derivará novamente os estágios afetados de cada arquivo das famílias impactadas, em todos os namespaces. Uma mudança em `chunker.py` afeta apenas `chunk`; uma mudança em `edges_treesitter.py` afeta `edge`, `chunk` e `guard`; uma mudança no parser afeta todos os estágios.

<a id="5-add-a-compiler-tier-optional"></a>

## 5. Adicionar uma camada do compilador (opcional)

Uma camada do compilador compensa quando as arestas da camada rápida são ambíguas (sobrecargas, despacho dinâmico, reexportações) e existe um indexador SCIP. `TIERS` em `server/code/domain/index/compiler_tiers.py` lista as camadas. Cada `TierSpec` nomeia o indexador (`scip-clang`, `scip-typescript`, `scip-go`, `scip-python`), a chave de marca-d'água em `code_index_state` (`scip_commit`, `scip_commit_ts`, `scip_commit_go`, `scip_commit_py`) e os workers que a movem. Uma nova camada precisa de:

1. Um harness que executa o indexador sobre os arquivos alterados de um intervalo de commits e converte
   sua saída para o delta JSON do Loom: `scripts/scip_harness/` (`run_delta.sh`,
   `run_typescript_delta.sh`, `run_python_delta.sh`, `run_go_monorepo_delta.sh`,
   `scip_to_delta.py`).
2. Um worker de delta (`server/code/workers/scip_ts_delta.py`, `scip_py_delta.py` e
   `scip_go_delta.py` são os modelos): seu `run_pass` entrega seu `_run_repo` a
   `run_tier` em `server/code/app/index/compiler_tier.py`, que cuida do loop de entradas, do isolamento de
   falhas e da reinicialização em execução completa. Ele vem com as configurações `LOOM_CODE_SCIP_<LANG>_DELTA_ENABLED` /
   `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS`
   ([adicionar um worker](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-worker.md)). Um produtor externo envia dados por
   `code.scip_ingest`, e o worker `code_scip_apply` aplica os lotes completos.
3. Uma chave de marca-d'água em `watermark_key_for` de `code_scip_apply` e uma linha de `TierSpec`, para que
   `code.index_status` e o painel informem a camada.

As linhas da camada do compilador carregam `tier='scip'`. As leituras as mesclam com a camada rápida por nome qualificado, dando preferência à camada do compilador. Os arquivos que o indexador ignora, e os commits anteriores à chegada do seu delta, continuam sendo lidos da camada rápida.

<a id="related"></a>

## Relacionados

- [Visão geral do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- [Arquitetura do índice de código](../architecture/code-index-architecture.md)
- [Operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
- [Estendendo o Loom](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
