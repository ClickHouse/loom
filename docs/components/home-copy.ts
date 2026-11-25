import type { SiteLocale } from "./site-locales";

type Capability = { title: string; detail: string; link: string };
type HomeCopy = {
  title: string;
  description: string;
  deployment: string;
  heading: string;
  lead: string;
  run: string;
  product: string;
  connect: string;
  sdk: string;
  screenshotAlt: string;
  screenshotCaption: string;
  contextHeading: string;
  contextDetail: string;
  memory: Capability;
  documents: Capability;
  code: Capability;
  useCasesHeading: string;
  useCasesDetail: string;
  useCasesLabel: string;
  softwareFactory: string;
  knowledgeFactory: string;
  autonomous: string;
  selfHosted: string;
  evaluationHeading: string;
  evaluationDetail: string;
  evaluate: string;
  deploymentOptions: string;
};

export const homeCopy: Record<SiteLocale, HomeCopy> = {
  en: {
    title: "Open-source agent memory and code intelligence | ClickHouse Loom",
    description:
      "Shared memory for Claude Code, Codex, Cursor, and autonomous agents. Recall lessons across sessions, find cited documents, and trace code.",
    deployment: "Open source · Self-hosted on ClickHouse",
    heading: "Shared memory for AI agents",
    lead: "Loom carries decisions and lessons across sessions. Agents can retrieve what the team learned, check cited documents, and see the source code each lesson applies to.",
    run: "Run Loom",
    product: "Explore the product",
    connect: "Connect coding agents",
    sdk: "Build with the SDKs",
    screenshotAlt:
      "Loom dashboard showing a payment decision linked to its function and a warning when related code has changed",
    screenshotCaption: "A saved decision points to the code it applies to.",
    contextHeading: "The context behind the next answer",
    contextDetail:
      "Agents can retrieve all three kinds of context through MCP, HTTP, or the Python and TypeScript SDKs.",
    memory: {
      title: "Shared memory",
      detail:
        "Carry decisions, constraints, and lessons into future sessions. Give people and agents a common place to review and update what they know.",
      link: "Memory for coding agents",
    },
    documents: {
      title: "Document search",
      detail:
        "Find the policy, runbook, or reference that answers a question. Open the cited passage and its document revision.",
      link: "Document search and RAG",
    },
    code: {
      title: "Code intelligence",
      detail:
        "Find symbols, trace callers, and link memories to the code they describe. See when the source has changed since a memory was recorded.",
      link: "Memory linked to code",
    },
    useCasesHeading: "Share what agents learn as they work",
    useCasesDetail:
      "Keep implementation decisions, review findings, and operational knowledge available to the next agent.",
    useCasesLabel: "Agent memory use cases",
    softwareFactory: "Memory for a software factory",
    knowledgeFactory: "Build a knowledge factory for AI agents",
    autonomous: "Memory for autonomous agents",
    selfHosted: "Self-hosted agent memory",
    evaluationHeading: "Evaluate Loom for your team",
    evaluationDetail: "Test recall, source links, and access controls with a workload from your team.",
    evaluate: "Run an evaluation",
    deploymentOptions: "Review deployment options",
  },
  de: {
    title: "Gemeinsames Gedächtnis für KI-Agenten | ClickHouse Loom",
    description:
      "Open-Source-Gedächtnis für KI-Agenten. Entscheidungen über Sitzungen hinweg abrufen, Dokumente mit Quellen finden und Wissen mit Quellcode verknüpfen.",
    deployment: "Open Source · Auf einer eigenen ClickHouse-Instanz",
    heading: "Gemeinsames Gedächtnis für KI-Agenten",
    lead: "Loom hält Entscheidungen und Erkenntnisse des Teams auch nach dem Ende einer Sitzung fest. Agenten können sie später abrufen, die Dokumentquellen prüfen und den zugehörigen Quellcode öffnen.",
    run: "Loom installieren",
    product: "Produkt kennenlernen",
    connect: "Coding-Agenten anbinden",
    sdk: "Mit den SDKs entwickeln",
    screenshotAlt:
      "Loom-Dashboard mit einer Entscheidung zum Zahlungsablauf, der verknüpften Funktion und einem Hinweis auf geänderten Code",
    screenshotCaption: "Die gespeicherte Entscheidung verweist auf die zugehörige Funktion.",
    contextHeading: "Welche Quellen stützen die nächste Antwort?",
    contextDetail: "Agenten greifen über MCP, HTTP oder die SDKs für Python und TypeScript auf diese Informationen zu.",
    memory: {
      title: "Gemeinsames Gedächtnis",
      detail:
        "Entscheidungen, Vorgaben und Erkenntnisse bleiben über Sitzungen hinweg verfügbar. Das Team und seine Agenten können sie prüfen und aktualisieren.",
      link: "Gedächtnis für Coding-Agenten",
    },
    documents: {
      title: "Dokumentensuche mit Quellen",
      detail:
        "Agenten finden die passende Richtlinie, Betriebsanleitung oder Referenz und öffnen den zitierten Abschnitt in der zugehörigen Dokumentversion.",
      link: "Dokumentensuche und RAG",
    },
    code: {
      title: "Quellcode analysieren",
      detail:
        "Symbole suchen, Funktionsaufrufe verfolgen und gespeicherte Erkenntnisse mit dem passenden Quellcode verknüpfen. Loom zeigt, wenn sich der Code seit dem Speichern geändert hat.",
      link: "Gedächtnis mit Quellcode verknüpfen",
    },
    useCasesHeading: "Wissen zwischen Agenten weitergeben",
    useCasesDetail:
      "Entscheidungen aus der Entwicklung, Erkenntnisse aus Code-Reviews und Betriebswissen stehen dem nächsten Agenten zur Verfügung.",
    useCasesLabel: "Einsatzbereiche für das Agentengedächtnis",
    softwareFactory: "Gedächtnis für eine Software Factory",
    knowledgeFactory: "Knowledge Factory für KI-Agenten",
    autonomous: "Gedächtnis für autonome Agenten",
    selfHosted: "Agentengedächtnis selbst betreiben",
    evaluationHeading: "Loom im eigenen Team testen",
    evaluationDetail: "Abruf, Quellenverweise und Zugriffsrechte mit einem Beispiel aus der eigenen Arbeit prüfen.",
    evaluate: "Loom testen",
    deploymentOptions: "Optionen für den Betrieb",
  },
  es: {
    title: "Memoria compartida para agentes de IA | ClickHouse Loom",
    description:
      "Memoria de código abierto para agentes de IA. Comparte decisiones entre sesiones, busca documentos con citas y vincula aprendizajes al código.",
    deployment: "Código abierto · Autoalojado en ClickHouse",
    heading: "Memoria compartida para agentes de IA",
    lead: "Loom conserva las decisiones y las lecciones del equipo entre sesiones. Los agentes pueden recuperar lo aprendido, consultar los documentos citados y abrir el código al que se refiere cada lección.",
    run: "Instalar Loom",
    product: "Explorar el producto",
    connect: "Conectar agentes de programación",
    sdk: "Desarrollar con los SDK",
    screenshotAlt:
      "Panel de Loom con una decisión sobre pagos vinculada a una función y un aviso de que el código relacionado cambió",
    screenshotCaption: "Cada decisión guardada puede señalar el código al que se aplica.",
    contextHeading: "El contexto para la próxima respuesta",
    contextDetail:
      "Los agentes pueden consultar estos tres tipos de contexto mediante MCP, HTTP o los SDK de Python y TypeScript.",
    memory: {
      title: "Memoria compartida",
      detail:
        "Conserva decisiones, restricciones y lecciones para otras sesiones. Da al equipo y a sus agentes un lugar común para revisarlas y actualizarlas.",
      link: "Memoria para agentes de programación",
    },
    documents: {
      title: "Búsqueda de documentos",
      detail:
        "Encuentra la política, el procedimiento o la referencia que responde a una pregunta. Abre el pasaje citado y comprueba la revisión del documento.",
      link: "Búsqueda de documentos y RAG",
    },
    code: {
      title: "Inteligencia de código",
      detail:
        "Localiza símbolos, sigue sus llamadas y vincula los recuerdos al código que describen. Comprueba si el código ha cambiado desde que se guardó el recuerdo.",
      link: "Memoria vinculada al código",
    },
    useCasesHeading: "Comparte lo que aprenden los agentes",
    useCasesDetail:
      "Deja las decisiones de implementación, los hallazgos de una revisión y el conocimiento operativo al alcance del siguiente agente.",
    useCasesLabel: "Casos de uso de la memoria para agentes",
    softwareFactory: "Memoria para una fábrica de software",
    knowledgeFactory: "Crear una fábrica de conocimiento para agentes de IA",
    autonomous: "Memoria para agentes autónomos",
    selfHosted: "Memoria autoalojada para agentes",
    evaluationHeading: "Evalúa Loom para tu equipo",
    evaluationDetail:
      "Prueba la recuperación, los vínculos a las fuentes y los permisos con una carga de trabajo de tu equipo.",
    evaluate: "Realizar una evaluación",
    deploymentOptions: "Ver opciones de despliegue",
  },
  fr: {
    title: "Mémoire partagée pour agents IA | ClickHouse Loom",
    description:
      "Mémoire open source pour agents IA. Retrouvez les décisions entre les sessions, recherchez des documents avec citations et reliez les acquis au code.",
    deployment: "Open source · À héberger sur votre instance ClickHouse",
    heading: "Mémoire partagée pour les agents IA",
    lead: "Loom conserve les décisions et les enseignements de l’équipe d’une session à l’autre. Les agents peuvent les retrouver, vérifier les documents cités et consulter le code concerné.",
    run: "Installer Loom",
    product: "Découvrir le produit",
    connect: "Connecter les agents de programmation",
    sdk: "Développer avec les SDK",
    screenshotAlt:
      "Tableau de bord Loom montrant une décision sur les paiements, la fonction associée et un avertissement lorsque le code a changé",
    screenshotCaption: "La décision enregistrée renvoie à la fonction concernée.",
    contextHeading: "Les sources derrière la prochaine réponse",
    contextDetail: "Les agents accèdent à ces informations via MCP, HTTP ou les SDK Python et TypeScript.",
    memory: {
      title: "Mémoire partagée",
      detail:
        "Conservez les décisions, les contraintes et les enseignements entre les sessions. L’équipe et ses agents disposent d’un espace commun pour les consulter et les mettre à jour.",
      link: "Mémoire pour agents de programmation",
    },
    documents: {
      title: "Recherche documentaire avec citations",
      detail:
        "Retrouvez la politique, la procédure ou la référence qui répond à la question. Consultez le passage cité dans la version du document utilisée.",
      link: "Recherche documentaire et RAG",
    },
    code: {
      title: "Analyse du code source",
      detail:
        "Retrouvez les symboles, suivez les appels et reliez les enseignements au code. Loom indique si le code a évolué depuis leur enregistrement.",
      link: "Relier la mémoire au code source",
    },
    useCasesHeading: "Transmettre les acquis d’un agent à l’autre",
    useCasesDetail:
      "Gardez les décisions de développement, les conclusions des revues de code et les connaissances d’exploitation à la portée du prochain agent.",
    useCasesLabel: "Cas d’usage de la mémoire pour agents IA",
    softwareFactory: "Mémoire pour une usine logicielle",
    knowledgeFactory: "Fabrique de connaissances pour agents IA",
    autonomous: "Mémoire pour agents autonomes",
    selfHosted: "Mémoire pour agents IA à auto-héberger",
    evaluationHeading: "Évaluer Loom dans votre équipe",
    evaluationDetail:
      "Vérifiez la recherche, les références aux sources et les droits d’accès sur un cas réel de votre équipe.",
    evaluate: "Évaluer Loom",
    deploymentOptions: "Options de déploiement",
  },
  it: {
    title: "Memoria condivisa per agenti AI | ClickHouse Loom",
    description:
      "Memoria open source per agenti AI. Conserva le decisioni tra le sessioni, trova documenti con citazioni e collega ciò che impari al codice.",
    deployment: "Open source · Da installare sulla propria istanza ClickHouse",
    heading: "Memoria condivisa per agenti AI",
    lead: "Loom conserva le decisioni e le informazioni che il team ha raccolto da una sessione all’altra. Gli agenti possono recuperarle, verificare i documenti citati e aprire il codice a cui si riferiscono.",
    run: "Installa Loom",
    product: "Scopri il prodotto",
    connect: "Collega gli agenti di programmazione",
    sdk: "Sviluppa con gli SDK",
    screenshotAlt:
      "Dashboard di Loom con una decisione sui pagamenti collegata alla funzione corrispondente e un avviso di modifica al codice",
    screenshotCaption: "La decisione salvata rimanda alla funzione a cui si applica.",
    contextHeading: "Le fonti della prossima risposta",
    contextDetail: "Gli agenti accedono a queste informazioni tramite MCP, HTTP o gli SDK per Python e TypeScript.",
    memory: {
      title: "Memoria condivisa",
      detail:
        "Conserva decisioni, vincoli e lezioni apprese tra le sessioni. Il team e gli agenti possono consultarli e aggiornarli nello stesso spazio.",
      link: "Memoria per agenti di programmazione",
    },
    documents: {
      title: "Ricerca nei documenti con citazioni",
      detail:
        "Trova la procedura, la guida operativa o il riferimento che risponde alla domanda. Apri il passaggio citato nella versione del documento usata.",
      link: "Ricerca nei documenti e RAG",
    },
    code: {
      title: "Analisi del codice sorgente",
      detail:
        "Trova i simboli, segui le chiamate e collega ciò che gli agenti imparano al codice. Loom mostra se il codice è cambiato dopo il salvataggio.",
      link: "Collega la memoria al codice",
    },
    useCasesHeading: "Condividi ciò che gli agenti imparano",
    useCasesDetail:
      "Rendi disponibili all’agente successivo le decisioni di sviluppo, i risultati delle revisioni del codice e le conoscenze operative.",
    useCasesLabel: "Casi d’uso della memoria per agenti AI",
    softwareFactory: "Memoria per una software factory",
    knowledgeFactory: "Fabbrica della conoscenza per agenti AI",
    autonomous: "Memoria per agenti autonomi",
    selfHosted: "Memoria per agenti da ospitare in autonomia",
    evaluationHeading: "Valuta Loom per il tuo team",
    evaluationDetail: "Verifica il recupero delle informazioni, le fonti e i permessi su un caso di lavoro del team.",
    evaluate: "Prova Loom",
    deploymentOptions: "Opzioni di installazione",
  },
  ja: {
    title: "AIエージェントの共有メモリとコード検索 | ClickHouse Loom",
    description:
      "AIエージェント向けのオープンソース共有メモリ。セッションをまたいで判断を引き継ぎ、出典付きで文書を検索し、知見をソースコードに結び付けます。",
    deployment: "オープンソース · ClickHouseでセルフホスト",
    heading: "AIエージェントの共有メモリ",
    lead: "Loomはチームの判断や知見を次のセッションへ引き継ぎます。エージェントは過去の知見を取り出し、出典となる文書や、その知見が適用されるソースコードを確認できます。",
    run: "Loomを起動する",
    product: "製品を見る",
    connect: "コーディングエージェントを接続",
    sdk: "SDKで開発する",
    screenshotAlt: "決済に関する判断と対応する関数を表示し、関連コードの変更を通知するLoomダッシュボード",
    screenshotCaption: "保存した判断から、適用先のコードを確認できます。",
    contextHeading: "次の回答に必要なコンテキスト",
    contextDetail:
      "エージェントはMCP、HTTP、またはPythonとTypeScriptのSDKを通じて、これら3種類のコンテキストを取得できます。",
    memory: {
      title: "共有メモリ",
      detail:
        "判断、制約、知見を次のセッションへ引き継ぎます。人とエージェントが同じ場所で内容を確認し、更新できます。",
      link: "コーディングエージェント向けメモリ",
    },
    documents: {
      title: "文書検索",
      detail: "質問に答えるポリシー、運用手順、参考資料を検索します。引用された箇所と文書のリビジョンを確認できます。",
      link: "文書検索とRAG",
    },
    code: {
      title: "コード分析",
      detail:
        "シンボルや呼び出し元を探し、知見を対象コードに結び付けます。知見を保存した後にソースが変わったか確認できます。",
      link: "コードに結び付くメモリ",
    },
    useCasesHeading: "エージェントが得た知見を共有",
    useCasesDetail: "実装の判断、レビュー結果、運用知識を次のエージェントが使える形で残します。",
    useCasesLabel: "エージェントメモリの活用例",
    softwareFactory: "ソフトウェアファクトリーのメモリ",
    knowledgeFactory: "AIエージェントのナレッジファクトリーを構築",
    autonomous: "自律エージェントのメモリ",
    selfHosted: "エージェントメモリをセルフホスト",
    evaluationHeading: "チームでLoomを評価する",
    evaluationDetail: "チームのワークロードで、記憶の検索、出典へのリンク、アクセス制御を検証します。",
    evaluate: "評価を実施する",
    deploymentOptions: "デプロイ方法を見る",
  },
  ko: {
    title: "AI 에이전트 공유 메모리와 코드 검색 | ClickHouse Loom",
    description:
      "AI 에이전트를 위한 오픈소스 공유 메모리. 세션 간에 결정을 공유하고, 출처가 표시된 문서를 검색하고, 학습한 내용을 코드에 연결합니다.",
    deployment: "오픈소스 · ClickHouse에서 자체 호스팅",
    heading: "AI 에이전트를 위한 공유 메모리",
    lead: "Loom은 팀의 결정과 배운 내용을 다음 세션으로 이어 줍니다. 에이전트는 이전 작업의 학습 내용을 불러오고, 인용된 문서와 해당 내용이 적용되는 소스 코드를 확인할 수 있습니다.",
    run: "Loom 실행하기",
    product: "제품 살펴보기",
    connect: "코딩 에이전트 연결하기",
    sdk: "SDK로 개발하기",
    screenshotAlt: "결제 관련 결정과 연결된 함수를 표시하고 관련 코드 변경을 알려 주는 Loom 대시보드",
    screenshotCaption: "저장된 결정에서 해당 코드로 이동할 수 있습니다.",
    contextHeading: "다음 답변에 필요한 컨텍스트",
    contextDetail: "에이전트는 MCP, HTTP, Python 및 TypeScript SDK로 세 가지 컨텍스트를 조회할 수 있습니다.",
    memory: {
      title: "공유 메모리",
      detail:
        "결정, 제약 조건, 배운 내용을 다음 세션에 전달합니다. 팀원과 에이전트가 같은 곳에서 내용을 검토하고 갱신할 수 있습니다.",
      link: "코딩 에이전트용 메모리",
    },
    documents: {
      title: "문서 검색",
      detail: "질문에 답하는 정책, 운영 절차, 참고 자료를 찾습니다. 인용된 구절과 문서 버전을 확인할 수 있습니다.",
      link: "문서 검색과 RAG",
    },
    code: {
      title: "코드 인텔리전스",
      detail:
        "심볼과 호출 위치를 찾아 메모리를 관련 코드에 연결합니다. 메모리를 저장한 뒤 소스가 변경되었는지 확인할 수 있습니다.",
      link: "코드에 연결된 메모리",
    },
    useCasesHeading: "에이전트가 배운 내용을 공유하세요",
    useCasesDetail: "구현 결정, 리뷰 결과, 운영 지식을 다음 에이전트도 활용할 수 있게 남깁니다.",
    useCasesLabel: "에이전트 메모리 사용 사례",
    softwareFactory: "소프트웨어 팩토리를 위한 메모리",
    knowledgeFactory: "AI 에이전트를 위한 지식 팩토리 구축",
    autonomous: "자율 에이전트용 메모리",
    selfHosted: "에이전트 메모리 자체 호스팅",
    evaluationHeading: "팀에서 Loom 평가하기",
    evaluationDetail: "팀의 실제 워크로드로 메모리 검색, 출처 링크, 접근 제어를 검증합니다.",
    evaluate: "평가 실행하기",
    deploymentOptions: "배포 옵션 살펴보기",
  },
  "pt-BR": {
    title: "Memória compartilhada para agentes de IA | ClickHouse Loom",
    description:
      "Memória de código aberto para agentes de IA. Compartilhe decisões entre sessões, pesquise documentos com citações e vincule aprendizados ao código.",
    deployment: "Código aberto · Hospedagem própria no ClickHouse",
    heading: "Memória compartilhada para agentes de IA",
    lead: "O Loom preserva as decisões e os aprendizados da equipe entre sessões. Os agentes podem recuperar esse contexto, conferir os documentos citados e consultar o código ao qual cada aprendizado se aplica.",
    run: "Executar o Loom",
    product: "Conhecer o produto",
    connect: "Conectar agentes de programação",
    sdk: "Desenvolver com os SDKs",
    screenshotAlt:
      "Painel do Loom com uma decisão de pagamento vinculada a uma função e um aviso de alteração no código relacionado",
    screenshotCaption: "Uma decisão salva aponta para o código ao qual se aplica.",
    contextHeading: "O contexto para a próxima resposta",
    contextDetail:
      "Os agentes podem consultar os três tipos de contexto por MCP, HTTP ou pelos SDKs de Python e TypeScript.",
    memory: {
      title: "Memória compartilhada",
      detail:
        "Leve decisões, restrições e aprendizados para outras sessões. A equipe e os agentes podem revisar e atualizar tudo no mesmo lugar.",
      link: "Memória para agentes de programação",
    },
    documents: {
      title: "Busca em documentos",
      detail:
        "Encontre a política, o procedimento ou a referência que responde à sua pergunta. Abra o trecho citado e confira a revisão do documento.",
      link: "Busca em documentos e RAG",
    },
    code: {
      title: "Inteligência de código",
      detail:
        "Encontre símbolos e chamadas, e vincule os aprendizados ao código que descrevem. Confira se o código mudou desde que a memória foi salva.",
      link: "Memória vinculada ao código",
    },
    useCasesHeading: "Compartilhe o que os agentes aprendem",
    useCasesDetail:
      "Deixe decisões de implementação, resultados de revisões e conhecimento operacional disponíveis para o próximo agente.",
    useCasesLabel: "Casos de uso de memória para agentes",
    softwareFactory: "Memória para uma fábrica de software",
    knowledgeFactory: "Crie uma fábrica de conhecimento para agentes de IA",
    autonomous: "Memória para agentes autônomos",
    selfHosted: "Memória para agentes com hospedagem própria",
    evaluationHeading: "Avalie o Loom com sua equipe",
    evaluationDetail:
      "Teste a recuperação de memória, os vínculos com as fontes e os controles de acesso em uma carga de trabalho da sua equipe.",
    evaluate: "Executar uma avaliação",
    deploymentOptions: "Ver opções de implantação",
  },
  ru: {
    title: "Общая память для ИИ-агентов | ClickHouse Loom",
    description:
      "Память с открытым кодом для ИИ-агентов. Сохраняйте решения между сеансами, находите документы с цитатами и связывайте выводы с кодом.",
    deployment: "Открытый исходный код · На своём экземпляре ClickHouse",
    heading: "Общая память для ИИ-агентов",
    lead: "Loom сохраняет решения команды и выводы агентов между сеансами. Агенты могут вспомнить нужное решение, проверить цитаты в документах и открыть исходный код, к которому оно относится.",
    run: "Установить Loom",
    product: "Обзор продукта",
    connect: "Подключить агентов для разработки",
    sdk: "Разработка с SDK",
    screenshotAlt:
      "Панель Loom с решением по обработке платежей, связанной функцией и предупреждением об изменении кода",
    screenshotCaption: "Сохранённое решение ссылается на функцию, к которой оно относится.",
    contextHeading: "Источники для следующего ответа",
    contextDetail: "Агенты получают эти сведения через MCP, HTTP или SDK для Python и TypeScript.",
    memory: {
      title: "Общая память",
      detail:
        "Сохраняйте решения, ограничения и выводы между сеансами. Команда и её агенты могут просматривать и обновлять их в одном месте.",
      link: "Память для агентов разработки",
    },
    documents: {
      title: "Поиск по документам с цитатами",
      detail:
        "Найдите правило, рабочую инструкцию или справочный материал. Откройте процитированный фрагмент в использованной версии документа.",
      link: "Поиск по документам и RAG",
    },
    code: {
      title: "Анализ исходного кода",
      detail:
        "Находите символы, прослеживайте вызовы функций и связывайте выводы агентов с исходным кодом. Loom показывает, изменился ли код после сохранения записи.",
      link: "Связать память с исходным кодом",
    },
    useCasesHeading: "Передавайте знания от агента к агенту",
    useCasesDetail:
      "Следующий агент может использовать решения по реализации, результаты проверки кода и эксплуатационные инструкции.",
    useCasesLabel: "Сценарии применения памяти для ИИ-агентов",
    softwareFactory: "Память для фабрики разработки ПО",
    knowledgeFactory: "Фабрика знаний для ИИ-агентов",
    autonomous: "Память для автономных агентов",
    selfHosted: "Память для агентов на своём сервере",
    evaluationHeading: "Проверьте Loom на задаче команды",
    evaluationDetail: "Проверьте поиск, ссылки на источники и права доступа на примере из работы команды.",
    evaluate: "Проверить Loom",
    deploymentOptions: "Варианты развёртывания",
  },
  "zh-CN": {
    title: "面向 AI 智能体的共享记忆与代码检索 | ClickHouse Loom",
    description: "面向 AI 智能体的开源共享记忆。跨会话共享团队决策，检索带引用的文档，并将经验关联到源代码。",
    deployment: "开源 · 基于 ClickHouse 自托管",
    heading: "面向 AI 智能体的共享记忆",
    lead: "Loom 让团队的决策和经验可以跨会话沿用。智能体能够找回已有经验，查阅带引用的文档，并查看每条经验适用的源代码。",
    run: "运行 Loom",
    product: "了解产品",
    connect: "连接编程智能体",
    sdk: "使用 SDK 开发",
    screenshotAlt: "Loom 仪表板显示与函数关联的支付决策，并提示相关代码已变更",
    screenshotCaption: "保存的决策可指向其适用的源代码。",
    contextHeading: "为下一次回答提供上下文",
    contextDetail: "智能体可以通过 MCP、HTTP 或 Python 和 TypeScript SDK 获取这三类上下文。",
    memory: {
      title: "共享记忆",
      detail: "将决策、约束和经验传递到后续会话。让团队成员和智能体在同一处审阅和更新这些信息。",
      link: "编程智能体的记忆",
    },
    documents: {
      title: "文档检索",
      detail: "查找能够回答问题的政策、操作手册或参考资料。打开引用段落并核对文档版本。",
      link: "文档检索与 RAG",
    },
    code: {
      title: "代码智能",
      detail: "查找符号和调用方，并把记忆关联到对应的代码。检查保存记忆后相关源代码是否发生变化。",
      link: "关联源代码的记忆",
    },
    useCasesHeading: "共享智能体在工作中积累的经验",
    useCasesDetail: "把实现决策、评审结果和运维知识留给接手工作的智能体。",
    useCasesLabel: "智能体记忆用例",
    softwareFactory: "软件工厂的共享记忆",
    knowledgeFactory: "为 AI 智能体构建知识工厂",
    autonomous: "自主智能体的记忆",
    selfHosted: "自托管智能体记忆",
    evaluationHeading: "为你的团队评估 Loom",
    evaluationDetail: "用团队的实际工作负载验证记忆召回、来源链接和访问控制。",
    evaluate: "开始评估",
    deploymentOptions: "查看部署选项",
  },
};
