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
  pilotChecks: { title: string; detail: string }[];
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
    pilotHeading: "Knowledge factory pilot scorecard",
    pilot:
      "Pick one recurring runbook question. Save a lesson that cites the approved rule, have the owner change the rule, and run these checks in a test namespace.",
    pilotChecks: [
      {
        title: "Current source",
        detail: "An agent in a fresh session returns the approved rule and cites the current document revision.",
      },
      {
        title: "Revision history",
        detail:
          "The revised lesson cites the document version the curator reviewed, and memory history shows the old wording.",
      },
      {
        title: "Namespace access",
        detail: "A reader without access to the namespace cannot retrieve the runbook or the saved lesson.",
      },
    ],
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
    pilotHeading: "Qué comprobar en la prueba piloto",
    pilot:
      "Elige una pregunta recurrente sobre un manual operativo. Guarda una lección que cite la regla aprobada, pide a la persona responsable que la modifique y realiza estas comprobaciones en un espacio de nombres de prueba.",
    pilotChecks: [
      {
        title: "Fuente vigente",
        detail: "En una sesión nueva, el agente devuelve la regla aprobada y cita la versión vigente del documento.",
      },
      {
        title: "Historial de versiones",
        detail:
          "La lección corregida cita la versión del documento que revisó la persona responsable de la lección, y el historial de memoria conserva el texto anterior.",
      },
      {
        title: "Acceso al espacio de nombres",
        detail:
          "Una identidad lectora sin acceso al espacio de nombres no puede recuperar el manual ni la lección guardada.",
      },
    ],
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
    pilotHeading: "知识工厂试点评估清单",
    pilot:
      "选择一个经常遇到的运维手册问题。保存一条引用已批准规则的经验，请手册负责人修改规则，然后在测试命名空间中完成以下检查。",
    pilotChecks: [
      {
        title: "当前来源",
        detail: "智能体在新会话中返回已批准的规则，并引用当前的文档版本。",
      },
      {
        title: "修订历史",
        detail: "修订后的经验引用整理人员审核过的版本，记忆历史仍可查看原文。",
      },
      {
        title: "命名空间权限",
        detail: "无权访问该命名空间的读取者无法检索手册或已保存的经验。",
      },
    ],
    evaluate: "规划评估",
    walkthrough: "查看知识工厂完整示例",
    englishLabel: "英文",
  },
} satisfies Record<string, KnowledgeFactoryCopy>;
