type Card = { title: string; detail: string; label: string };
type EnterpriseCopy = {
  title: string;
  description: string;
  heading: string;
  lead: string;
  englishLabel: string;
  plan: string;
  security: string;
  deploymentHeading: string;
  deploymentIntro: string;
  docker: Card;
  kubernetes: Card;
  controlsHeading: string;
  controlsIntro: string;
  identity: Card;
  audit: Card;
  erasure: Card;
  backup: Card;
  evaluateHeading: string;
  evaluateIntro: string;
  evaluate: string;
  benchmarks: string;
  monitoring: string;
  boundary: string;
};

export const enterpriseCopy = {
  en: {
    title: "Self-hosted agent memory for teams | ClickHouse Loom",
    description:
      "Evaluate self-hosted agent memory on ClickHouse. Choose Docker or Kubernetes, scope team access, and check storage, models, audit and erasure.",
    heading: "Run Loom for your team",
    lead: "Self-host Loom on ClickHouse in infrastructure you operate. Set who can access shared knowledge and choose the storage and model providers your deployment uses.",
    englishLabel: "",
    plan: "Plan a deployment",
    security: "Review the security model",
    deploymentHeading: "Choose how to deploy",
    deploymentIntro: "Use Docker for an evaluation or configure a Kubernetes deployment with the Helm chart.",
    docker: {
      title: "Docker",
      detail: "Start a self-hosted instance, connect an agent, and evaluate a workflow on your own infrastructure.",
      label: "Run the quickstart",
    },
    kubernetes: {
      title: "Kubernetes",
      detail:
        "Use the Helm chart to configure services, storage, authentication, and the ingress for a team deployment.",
      label: "Deploy with Helm",
    },
    controlsHeading: "Evaluate operational controls",
    controlsIntro: "Review the settings and responsibilities for running Loom with shared access.",
    identity: {
      title: "Identity and access",
      detail: "Connect an OpenID Connect provider and scope access to team namespaces with roles, grants, and tokens.",
      label: "Review sign-in and configuration",
    },
    audit: {
      title: "Audit and usage",
      detail: "Inspect changes to memory and trace operations, model usage, and costs in your deployment.",
      label: "Read about the audit log",
    },
    erasure: {
      title: "Retention and erasure",
      detail:
        "Set expiry where appropriate and use the erasure workflow to track deletion and check for remaining data.",
      label: "Review retention and erasure",
    },
    backup: {
      title: "Backups and recovery",
      detail: "Plan backup, restore, and monitoring for the stores and services your deployment uses.",
      label: "Plan backup and recovery",
    },
    evaluateHeading: "Evaluate on your workload",
    evaluateIntro:
      "Test the retrieval quality, latency, and operating cost for the agents and repositories you plan to run.",
    evaluate: "Run an evaluation",
    benchmarks: "See published benchmarks",
    monitoring: "Review monitoring",
    boundary: "Check where data and model requests go",
  },
  de: {
    title: "KI-Agentengedächtnis selbst hosten | ClickHouse Loom",
    description:
      "Betreiben Sie ein gemeinsames Gedächtnis für KI-Agenten mit ClickHouse. Prüfen Sie Docker, Kubernetes, Zugriffsrechte und lokale Modelle.",
    heading: "Gedächtnis für KI-Agenten selbst hosten",
    lead: "Mit Loom teilen Coding-Agenten Entscheidungen und Erkenntnisse über Sitzungen hinweg. Betreiben Sie Loom mit ClickHouse auf Ihrer Infrastruktur und legen Sie fest, wer auf gespeichertes Wissen zugreift und welche Modelle es verarbeiten.",
    englishLabel: "Englisch",
    plan: "Bereitstellung planen",
    security: "Sicherheitsmodell prüfen",
    deploymentHeading: "Die passende Bereitstellung wählen",
    deploymentIntro:
      "Testen Sie einen Arbeitsablauf mit Docker. Für den Betrieb im Team können Sie Loom mit dem Helm-Chart in Kubernetes bereitstellen.",
    docker: {
      title: "Docker",
      detail:
        "Starten Sie eine eigene Instanz, verbinden Sie einen Agenten und testen Sie den Abruf gespeicherter Entscheidungen in einer neuen Sitzung.",
      label: "Schnellstart öffnen",
    },
    kubernetes: {
      title: "Kubernetes",
      detail: "Konfigurieren Sie Dienste, Speicher, Anmeldung und Ingress mit dem Helm-Chart.",
      label: "Mit Helm bereitstellen",
    },
    controlsHeading: "Zugriff und Betrieb prüfen",
    controlsIntro:
      "Klären Sie, wer Wissen lesen und ändern darf, wie lange Daten gespeichert werden und wie die Wiederherstellung abläuft.",
    identity: {
      title: "Identität und Zugriff",
      detail:
        "Binden Sie einen OpenID-Connect-Anbieter an und begrenzen Sie mit Rollen, Berechtigungen und Tokens den Zugriff auf die Namensräume des Teams.",
      label: "Anmeldung konfigurieren",
    },
    audit: {
      title: "Audit und Nutzung",
      detail:
        "Prüfen Sie Änderungen an gespeicherten Einträgen sowie Vorgänge, Modellnutzung und Kosten Ihrer Bereitstellung.",
      label: "Auditprotokoll prüfen",
    },
    erasure: {
      title: "Aufbewahrung und Löschung",
      detail:
        "Legen Sie bei Bedarf Ablaufzeiten fest. Verfolgen Sie Löschvorgänge und prüfen Sie, welche Daten noch vorhanden sind.",
      label: "Aufbewahrung und Löschung prüfen",
    },
    backup: {
      title: "Sicherung und Wiederherstellung",
      detail:
        "Planen Sie Sicherung, Wiederherstellung und Überwachung für die Datenbanken und Dienste Ihrer Bereitstellung.",
      label: "Wiederherstellung planen",
    },
    evaluateHeading: "Mit eigenen Aufgaben testen",
    evaluateIntro:
      "Messen Sie die Qualität und Latenz des Abrufs sowie die Betriebskosten für Ihre Agenten und Repositories. Prüfen Sie separat, welche Ziele Ihre Modellanfragen erreichen.",
    evaluate: "Loom im eigenen Team testen",
    benchmarks: "Veröffentlichte Benchmarks ansehen",
    monitoring: "Überwachung prüfen",
    boundary: "Speicherorte und Modellanfragen prüfen",
  },
  fr: {
    title: "Mémoire auto-hébergée pour agents IA | ClickHouse Loom",
    description:
      "Évaluez une mémoire partagée auto-hébergée pour agents IA avec ClickHouse. Vérifiez le déploiement, les accès, les modèles et l’audit.",
    heading: "Mémoire auto-hébergée pour agents IA",
    lead: "Avec Loom, les agents de programmation retrouvent les décisions et les enseignements de l’équipe d’une session à l’autre. Hébergez Loom avec ClickHouse sur votre infrastructure et choisissez qui accède aux données et quels modèles les traitent.",
    englishLabel: "en anglais",
    plan: "Préparer le déploiement",
    security: "Consulter le modèle de sécurité",
    deploymentHeading: "Choisir un mode de déploiement",
    deploymentIntro:
      "Testez un cas d’usage avec Docker. Pour un déploiement d’équipe sur Kubernetes, utilisez le chart Helm.",
    docker: {
      title: "Docker",
      detail:
        "Démarrez une instance, connectez un agent et vérifiez qu’il retrouve une décision enregistrée depuis une autre session.",
      label: "Ouvrir le guide de démarrage",
    },
    kubernetes: {
      title: "Kubernetes",
      detail: "Configurez les services, le stockage, l’authentification et l’ingress avec le chart Helm.",
      label: "Déployer avec Helm",
    },
    controlsHeading: "Vérifier les accès et l’exploitation",
    controlsIntro:
      "Déterminez qui peut lire et modifier les données, combien de temps les conserver et comment les restaurer.",
    identity: {
      title: "Identité et accès",
      detail:
        "Connectez un fournisseur OpenID Connect et limitez l’accès aux espaces de noms de l’équipe avec des rôles, des autorisations et des jetons.",
      label: "Configurer la connexion",
    },
    audit: {
      title: "Audit et utilisation",
      detail:
        "Consultez l’historique des modifications et vérifiez les opérations, l’utilisation des modèles et les coûts.",
      label: "Consulter le journal d’audit",
    },
    erasure: {
      title: "Conservation et effacement",
      detail:
        "Fixez des durées de conservation si nécessaire. Suivez les demandes d’effacement et vérifiez les données restantes.",
      label: "Vérifier les règles de conservation",
    },
    backup: {
      title: "Sauvegarde et restauration",
      detail:
        "Planifiez la sauvegarde, la restauration et la supervision des bases de données et des services utilisés.",
      label: "Préparer la restauration",
    },
    evaluateHeading: "Évaluer Loom sur vos propres tâches",
    evaluateIntro:
      "Mesurez la qualité des résultats, la latence et le coût pour vos agents et vos dépôts. Vérifiez séparément la destination des requêtes envoyées aux modèles.",
    evaluate: "Évaluer Loom dans votre équipe",
    benchmarks: "Voir les benchmarks publiés",
    monitoring: "Examiner la supervision",
    boundary: "Vérifier le stockage et les appels aux modèles",
  },
  es: {
    title: "Memoria autoalojada para agentes de IA | ClickHouse Loom",
    description:
      "Evalúa memoria compartida autoalojada para agentes de programación. Revisa los accesos, el almacenamiento y los modelos en tu infraestructura.",
    heading: "Memoria autoalojada para agentes de IA",
    lead: "Loom comparte las decisiones y los aprendizajes del equipo entre agentes de programación y sesiones. Aloja Loom con ClickHouse en tu infraestructura y decide quién puede consultar el conocimiento y qué modelos lo procesan.",
    englishLabel: "en inglés",
    plan: "Planificar el despliegue",
    security: "Revisar el modelo de seguridad",
    deploymentHeading: "Elegir cómo desplegar Loom",
    deploymentIntro:
      "Prueba un flujo de trabajo con Docker o prepara un despliegue de equipo en Kubernetes con el chart de Helm.",
    docker: {
      title: "Docker",
      detail:
        "Inicia una instancia, conecta un agente y comprueba que recupera una decisión guardada desde una sesión nueva.",
      label: "Abrir la guía de inicio rápido",
    },
    kubernetes: {
      title: "Kubernetes",
      detail: "Configura los servicios, el almacenamiento, la autenticación y el ingress con el chart de Helm.",
      label: "Desplegar con Helm",
    },
    controlsHeading: "Revisar el acceso y la operación",
    controlsIntro: "Define quién puede consultar y modificar los datos, cuánto tiempo se guardan y cómo se recuperan.",
    identity: {
      title: "Identidad y acceso",
      detail:
        "Conecta un proveedor OpenID Connect y limita el acceso a los espacios de nombres del equipo con roles, permisos y tokens.",
      label: "Configurar el inicio de sesión",
    },
    audit: {
      title: "Auditoría y uso",
      detail: "Consulta el historial de cambios, las operaciones, el uso de los modelos y los costes de tu despliegue.",
      label: "Consultar el registro de auditoría",
    },
    erasure: {
      title: "Conservación y borrado",
      detail: "Configura los plazos de conservación. Sigue el proceso de borrado y comprueba qué datos quedan.",
      label: "Revisar la conservación y el borrado",
    },
    backup: {
      title: "Copias de seguridad y recuperación",
      detail: "Planifica las copias, la restauración y la supervisión de las bases de datos y los servicios.",
      label: "Preparar la recuperación",
    },
    evaluateHeading: "Evaluar Loom con tareas de tu equipo",
    evaluateIntro:
      "Mide la calidad de la recuperación, la latencia y el coste con tus agentes y repositorios. Comprueba también dónde se procesan las solicitudes a los modelos.",
    evaluate: "Evaluar Loom",
    benchmarks: "Ver las pruebas de rendimiento publicadas",
    monitoring: "Revisar la supervisión",
    boundary: "Comprobar el almacenamiento y las llamadas a los modelos",
  },
  "pt-BR": {
    title: "Memória auto-hospedada para agentes de IA | ClickHouse Loom",
    description:
      "Avalie memória compartilhada auto-hospedada para agentes de programação. Confira acessos, armazenamento e modelos na sua infraestrutura.",
    heading: "Memória auto-hospedada para agentes de IA",
    lead: "O Loom mantém as decisões e os aprendizados da equipe disponíveis entre agentes de programação e sessões. Hospede o Loom com ClickHouse na sua infraestrutura e controle quem pode acessar o conhecimento e quais modelos o processam.",
    englishLabel: "em inglês",
    plan: "Planejar a implantação",
    security: "Revisar o modelo de segurança",
    deploymentHeading: "Escolher como implantar o Loom",
    deploymentIntro:
      "Teste um fluxo de trabalho com Docker ou configure uma implantação para a equipe no Kubernetes com o chart do Helm.",
    docker: {
      title: "Docker",
      detail: "Inicie uma instância, conecte um agente e confira se ele recupera uma decisão salva em uma nova sessão.",
      label: "Abrir o guia de início rápido",
    },
    kubernetes: {
      title: "Kubernetes",
      detail: "Configure serviços, armazenamento, autenticação e ingress com o chart do Helm.",
      label: "Implantar com Helm",
    },
    controlsHeading: "Revisar o acesso e a operação",
    controlsIntro:
      "Defina quem pode ler e alterar os dados, por quanto tempo eles ficam armazenados e como restaurá-los.",
    identity: {
      title: "Identidade e acesso",
      detail:
        "Conecte um provedor OpenID Connect e limite o acesso aos namespaces da equipe com papéis, permissões e tokens.",
      label: "Configurar o login",
    },
    audit: {
      title: "Auditoria e uso",
      detail: "Consulte o histórico de alterações, as operações, o uso dos modelos e os custos da sua implantação.",
      label: "Consultar o registro de auditoria",
    },
    erasure: {
      title: "Retenção e exclusão",
      detail: "Defina os prazos de retenção. Acompanhe a exclusão e confira quais dados ainda permanecem.",
      label: "Revisar a retenção e a exclusão",
    },
    backup: {
      title: "Backup e recuperação",
      detail: "Planeje o backup, a restauração e o monitoramento dos bancos de dados e serviços.",
      label: "Planejar a recuperação",
    },
    evaluateHeading: "Avaliar o Loom com tarefas da equipe",
    evaluateIntro:
      "Meça a qualidade da recuperação, a latência e o custo com seus agentes e repositórios. Confira também para onde vão as solicitações aos modelos.",
    evaluate: "Avaliar o Loom",
    benchmarks: "Ver os benchmarks publicados",
    monitoring: "Revisar o monitoramento",
    boundary: "Conferir o armazenamento e as chamadas aos modelos",
  },
} satisfies Record<string, EnterpriseCopy>;
