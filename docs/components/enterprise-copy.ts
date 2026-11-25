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
  ja: {
    title: "AIエージェントの共有メモリをセルフホスト | ClickHouse Loom",
    description:
      "AIコーディングエージェントの共有メモリを自社環境で運用。DockerやKubernetesでの導入、アクセス権限、保存先、モデルへの送信先を確認できます。",
    heading: "AIエージェントの共有メモリをセルフホスト",
    lead: "Loomで、チームの判断や作業で得た知見をコーディングエージェント間で共有できます。自社のインフラでClickHouseとLoomを運用し、保存した情報へのアクセス権限と処理に使うモデルを設定できます。",
    englishLabel: "英語",
    plan: "導入を計画する",
    security: "セキュリティモデルを確認する",
    deploymentHeading: "導入方法を選ぶ",
    deploymentIntro: "Dockerで作業の流れを試せます。チームでKubernetesを使う場合はHelmチャートで構成します。",
    docker: {
      title: "Docker",
      detail: "インスタンスを起動してエージェントを接続し、別のセッションで保存済みの判断を取得できるか確認します。",
      label: "クイックスタートを開く",
    },
    kubernetes: {
      title: "Kubernetes",
      detail: "Helmチャートでサービス、ストレージ、認証、Ingressを構成します。",
      label: "Helmでデプロイする",
    },
    controlsHeading: "アクセス権限と運用を確認する",
    controlsIntro: "誰が情報を読み書きできるか、データをどれだけ保持するか、障害時にどう復旧するかを決めます。",
    identity: {
      title: "認証とアクセス制御",
      detail: "OpenID Connectプロバイダーを接続し、ロール、権限、トークンでチームの名前空間へのアクセスを制御します。",
      label: "ログインを設定する",
    },
    audit: {
      title: "監査と利用状況",
      detail: "保存した情報の変更履歴、操作、モデルの利用量、コストを確認します。",
      label: "監査ログを確認する",
    },
    erasure: {
      title: "保持と消去",
      detail: "必要に応じて保持期間を設定します。消去処理の進行状況と残っているデータを確認できます。",
      label: "保持と消去の手順を確認する",
    },
    backup: {
      title: "バックアップと復旧",
      detail: "利用するデータベースやサービスのバックアップ、復元、監視を計画します。",
      label: "復旧を計画する",
    },
    evaluateHeading: "チームの実際のタスクで評価する",
    evaluateIntro:
      "利用予定のエージェントとリポジトリで検索品質、応答時間、運用コストを測定します。モデルへのリクエストの送信先も確認します。",
    evaluate: "Loomを評価する",
    benchmarks: "公開ベンチマークを見る",
    monitoring: "監視方法を確認する",
    boundary: "データの保存先とモデルへの送信先を確認する",
  },
  ko: {
    title: "AI 에이전트 공유 메모리 자체 호스팅 | ClickHouse Loom",
    description:
      "코딩 에이전트의 공유 메모리를 자체 인프라에서 운영하세요. Docker와 Kubernetes 배포, 접근 권한, 저장 위치와 모델 요청의 전송 대상을 확인할 수 있습니다.",
    heading: "AI 에이전트 공유 메모리 자체 호스팅",
    lead: "Loom을 사용하면 팀의 결정과 작업에서 얻은 지식을 여러 코딩 에이전트와 세션에서 공유할 수 있습니다. 자체 인프라에서 ClickHouse와 Loom을 운영하고 저장된 정보에 접근할 수 있는 주체와 이를 처리할 모델을 설정하세요.",
    englishLabel: "영어",
    plan: "배포 계획 세우기",
    security: "보안 모델 확인하기",
    deploymentHeading: "배포 방식 선택하기",
    deploymentIntro:
      "Docker에서 작업 흐름을 시험해 보세요. 팀에서 Kubernetes를 사용한다면 Helm 차트로 배포를 구성할 수 있습니다.",
    docker: {
      title: "Docker",
      detail: "인스턴스를 시작하고 에이전트를 연결한 뒤, 새 세션에서 저장한 결정을 다시 찾을 수 있는지 확인합니다.",
      label: "빠른 시작 가이드 열기",
    },
    kubernetes: {
      title: "Kubernetes",
      detail: "Helm 차트로 서비스, 스토리지, 인증, 인그레스를 설정합니다.",
      label: "Helm으로 배포하기",
    },
    controlsHeading: "접근 권한과 운영 방식 확인하기",
    controlsIntro:
      "누가 정보를 읽고 수정할 수 있는지, 데이터를 얼마나 보관하는지, 장애가 났을 때 어떻게 복구할지 정합니다.",
    identity: {
      title: "인증과 접근 제어",
      detail: "OpenID Connect 공급자를 연결하고 역할, 권한, 토큰으로 팀 네임스페이스에 대한 접근을 제한합니다.",
      label: "로그인 설정하기",
    },
    audit: {
      title: "감사와 사용량",
      detail: "저장된 정보의 변경 이력, 작업 내역, 모델 사용량과 비용을 확인합니다.",
      label: "감사 로그 확인하기",
    },
    erasure: {
      title: "보존과 삭제",
      detail: "필요한 경우 보존 기간을 설정합니다. 삭제 진행 상황을 추적하고 남은 데이터가 있는지 확인할 수 있습니다.",
      label: "보존과 삭제 절차 확인하기",
    },
    backup: {
      title: "백업과 복구",
      detail: "사용하는 데이터베이스와 서비스의 백업, 복원, 모니터링을 계획합니다.",
      label: "복구 계획 세우기",
    },
    evaluateHeading: "팀의 실제 작업으로 평가하기",
    evaluateIntro:
      "사용할 에이전트와 저장소로 검색 품질, 응답 시간, 운영 비용을 측정합니다. 모델 요청이 어디로 전송되는지도 확인합니다.",
    evaluate: "Loom 평가하기",
    benchmarks: "공개 벤치마크 보기",
    monitoring: "모니터링 방법 확인하기",
    boundary: "데이터 저장 위치와 모델 요청의 전송 대상 확인하기",
  },
} satisfies Record<string, EnterpriseCopy>;
