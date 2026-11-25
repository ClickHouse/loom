type EvidenceCard = { title: string; detail: string; label: string };

type KnowledgeFactoryCopy = {
  title: string;
  description: string;
  heading: string;
  lead: string;
  evidenceHeading: string;
  source: EvidenceCard;
  lesson: EvidenceCard;
  code: EvidenceCard;
  pilotHeading: string;
  pilot: string;
  evaluate: string;
  walkthrough: string;
  englishLabel: string;
};

export const knowledgeFactoryCopy = {
  en: {
    title: "Evaluate an AI knowledge factory | ClickHouse Loom",
    description:
      "Use Loom to evaluate a knowledge factory for AI agents. Publish team documents, share reviewed lessons, and test source revisions and access.",
    heading: "Evaluate a knowledge factory for AI agents",
    lead: "A knowledge factory turns approved documents and lessons from agent work into knowledge the next agent can use. Loom stores each source and decision with its provenance so teams can review what changes.",
    evidenceHeading: "Give the next agent evidence it can check",
    source: {
      title: "Publish the source",
      detail:
        "Index an approved runbook or policy. An agent can cite the passage it used and identify the document revision.",
      label: "Search documents with citations",
    },
    lesson: {
      title: "Save a lesson",
      detail:
        "Keep a short, reviewed decision in shared memory. Link it to its source and retain the writer and revision history.",
      label: "Share memory across sessions",
    },
    code: {
      title: "Review source changes",
      detail:
        "Link an implementation lesson to the definition it describes. When the definition changes, check whether the lesson still applies.",
      label: "Link memory to code",
    },
    pilotHeading: "Test one runbook change",
    pilot:
      "In a test namespace, publish a rollback rule for five failed health checks within 60 seconds. Save a lesson that cites the rule, then change the threshold to three. Ask a fresh agent to cite the current rule. Revise the lesson and check its history. Confirm that a reader outside the namespace cannot retrieve the source.",
    evaluate: "Plan a workload evaluation",
    walkthrough: "Run the knowledge factory walkthrough",
    englishLabel: "",
  },
  es: {
    title: "Fábrica de conocimiento para agentes de IA | ClickHouse Loom",
    description:
      "Evalúa una fábrica de conocimiento para agentes de IA con Loom. Comparte documentos y lecciones revisadas y comprueba sus fuentes, versiones y permisos.",
    heading: "Evalúa una fábrica de conocimiento para agentes de IA",
    lead: "Una fábrica de conocimiento convierte documentos aprobados y lecciones del trabajo de los agentes en información que el siguiente agente puede reutilizar. Loom conserva el origen de cada fuente y decisión para que el equipo pueda revisar los cambios.",
    evidenceHeading: "Da al siguiente agente fuentes que pueda comprobar",
    source: {
      title: "Publica la fuente",
      detail:
        "Indexa un manual operativo o una política aprobada. El agente puede citar el pasaje que usó e identificar la versión del documento.",
      label: "Buscar documentos con citas",
    },
    lesson: {
      title: "Guarda una lección",
      detail:
        "Guarda una decisión breve y revisada en la memoria compartida. Vincúlala a su fuente y conserva su autor y su historial de versiones.",
      label: "Compartir memoria entre sesiones",
    },
    code: {
      title: "Revisa los cambios en el código",
      detail:
        "Vincula una lección de implementación a la definición que describe. Si cambia la definición, comprueba si la lección sigue siendo válida.",
      label: "Vincular la memoria al código",
    },
    pilotHeading: "Prueba un cambio en un manual operativo",
    pilot:
      "En un espacio de nombres de prueba, publica una regla de reversión para cinco comprobaciones de estado fallidas en 60 segundos. Guarda una lección que la cite y cambia el umbral a tres fallos. Pide a un agente en una sesión nueva que cite la regla vigente. Corrige la lección y consulta el historial. Comprueba que una identidad ajena al espacio de nombres no pueda recuperar la fuente.",
    evaluate: "Planificar una evaluación",
    walkthrough: "Seguir el ejemplo de principio a fin",
    englishLabel: "inglés",
  },
  "zh-CN": {
    title: "评估 AI 智能体知识工厂 | ClickHouse Loom",
    description: "使用 Loom 评估 AI 智能体知识工厂。共享经过审核的文档与经验，核对引用来源、文档版本和访问权限。",
    heading: "评估面向 AI 智能体的知识工厂",
    lead: "知识工厂把已批准的文档和智能体工作中积累的经验整理成可复用的知识。Loom 保存每条决策及其来源，供团队审核变更，也让后续智能体能够检索。",
    evidenceHeading: "让后续智能体获得可核对的依据",
    source: {
      title: "发布来源文档",
      detail: "索引已批准的运维手册或政策文档。智能体可以引用使用过的段落，并指出文档版本。",
      label: "检索并引用文档",
    },
    lesson: {
      title: "保存一条经验",
      detail: "将一条经过审核的简短决策存入共享记忆，关联来源，并保留写入者和版本历史。",
      label: "跨会话共享记忆",
    },
    code: {
      title: "复核代码变更",
      detail: "将实现经验关联到它描述的代码定义。定义变更后，对照当前代码确认原经验是否仍然适用。",
      label: "将记忆关联到代码",
    },
    pilotHeading: "用一条运维规则验证流程",
    pilot:
      "在测试命名空间中发布一条在 60 秒内五次健康检查失败后触发回滚的规则，保存引用该规则的经验。将阈值改为三次，请智能体在新会话中引用当前规则。修订已保存的经验并检查历史记录。确认命名空间之外的身份无法读取该来源。",
    evaluate: "规划评估",
    walkthrough: "查看知识工厂完整示例",
    englishLabel: "英文",
  },
} satisfies Record<string, KnowledgeFactoryCopy>;
