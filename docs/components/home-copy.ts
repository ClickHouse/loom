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
    title: "Shared agent memory and code intelligence | ClickHouse Loom",
    description:
      "Shared memory for Claude Code, Codex, Cursor, and autonomous agents. Recall lessons across sessions, find cited documents, and trace code.",
    deployment: "Open source · Self-hosted on ClickHouse",
    heading: "Shared memory for AI agents",
    lead: "Give agents access to past decisions, cited documents, and the source code behind each lesson.",
    run: "Install Loom",
    product: "Explore the product",
    connect: "Connect coding agents",
    sdk: "Build with the SDKs",
    screenshotAlt:
      "Loom dashboard showing a payment decision linked to its function and a warning when related code has changed",
    screenshotCaption: "A saved decision points to the code it applies to.",
    contextHeading: "What agents can retrieve",
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
  ar: {
    title: "ذاكرة مشتركة لوكلاء الذكاء الاصطناعي | ClickHouse Loom",
    description:
      "ذاكرة مفتوحة المصدر لوكلاء الذكاء الاصطناعي. احتفظ بقرارات الفريق بين الجلسات، وابحث في المستندات مع ذكر المصادر، واربط المعرفة بالشيفرة.",
    deployment: "مفتوح المصدر · استضافة ذاتية على ClickHouse",
    heading: "ذاكرة مشتركة لوكلاء الذكاء الاصطناعي",
    lead: "يحتفظ Loom بقرارات الفريق وما يتعلمه الوكلاء من جلسة إلى أخرى. يمكن للوكلاء استرجاع تلك المعرفة، والتحقق من المقاطع المستشهد بها، وفتح الشيفرة المرتبطة بها.",
    run: "تثبيت Loom",
    product: "استكشف المنتج",
    connect: "ربط وكلاء البرمجة",
    sdk: "التطوير باستخدام حزم SDK",
    screenshotAlt: "لوحة Loom تعرض قرارًا بشأن المدفوعات مرتبطًا بالدالة المعنية، مع تنبيه إلى تغيير في الشيفرة",
    screenshotCaption: "يشير القرار المحفوظ إلى الدالة التي ينطبق عليها.",
    contextHeading: "المصادر التي يستند إليها الرد التالي",
    contextDetail: "يستطيع الوكلاء الوصول إلى هذه المعلومات عبر MCP أو HTTP أو حزم SDK للغتي Python وTypeScript.",
    memory: {
      title: "ذاكرة مشتركة",
      detail:
        "احتفظ بالقرارات والقيود والدروس المستفادة بين الجلسات. يستطيع الفريق ووكلاؤه مراجعتها وتحديثها في مكان واحد.",
      link: "ذاكرة لوكلاء البرمجة",
    },
    documents: {
      title: "البحث في المستندات مع إظهار المصادر",
      detail:
        "اعثر على السياسة أو الإجراء أو المرجع الذي يجيب عن السؤال. افتح المقطع المستشهد به في نسخة المستند التي استُخدمت.",
      link: "البحث في المستندات وتقنية RAG",
    },
    code: {
      title: "تحليل الشيفرة المصدرية",
      detail:
        "ابحث عن الرموز البرمجية وتتبّع استدعاءات الدوال واربط ما يتعلمه الوكلاء بالشيفرة. يبيّن Loom ما إذا كانت الشيفرة قد تغيّرت منذ حفظ المعلومة.",
      link: "ربط الذاكرة بالشيفرة",
    },
    useCasesHeading: "شارك ما يتعلمه الوكلاء أثناء العمل",
    useCasesDetail: "أتِح للوكيل التالي قرارات التنفيذ ونتائج مراجعة الشيفرة والمعرفة اللازمة للتشغيل.",
    useCasesLabel: "حالات استخدام ذاكرة الوكلاء",
    softwareFactory: "ذاكرة لمصنع البرمجيات",
    knowledgeFactory: "مصنع معرفة لوكلاء الذكاء الاصطناعي",
    autonomous: "ذاكرة للوكلاء المستقلين",
    selfHosted: "ذاكرة للوكلاء باستضافة ذاتية",
    evaluationHeading: "قيّم Loom على مهمة من عمل فريقك",
    evaluationDetail: "اختبر الاسترجاع وروابط المصادر وصلاحيات الوصول على مثال من عمل الفريق.",
    evaluate: "جرّب Loom",
    deploymentOptions: "خيارات النشر",
  },
  cs: {
    title: "Sdílená paměť pro AI agenty | ClickHouse Loom",
    description:
      "Dlouhodobá paměť pro programovací agenty na vlastním ClickHouse. Uchovávejte rozhodnutí mezi relacemi, hledejte v dokumentech a propojujte poznatky s kódem.",
    deployment: "Otevřený zdrojový kód · Provoz na vlastním ClickHouse",
    heading: "Sdílená paměť pro AI agenty",
    lead: "Loom uchovává týmová rozhodnutí a poznatky mezi relacemi. Agenti mohou dohledat, co už tým zjistil, ověřit citované dokumenty a otevřít související zdrojový kód.",
    run: "Nainstalovat Loom (anglicky)",
    product: "Prohlédnout funkce",
    connect: "Připojit programovací agenty (anglicky)",
    sdk: "Vyvíjet pomocí SDK (anglicky)",
    screenshotAlt: "Přehled Loomu s rozhodnutím o platbách, odkazem na související funkci a upozorněním na změnu kódu",
    screenshotCaption: "Uložené rozhodnutí odkazuje na funkci, které se týká.",
    contextHeading: "Podklady pro další odpověď",
    contextDetail: "Agenti mohou pracovat s pamětí, dokumenty i kódem přes MCP, HTTP nebo SDK pro Python a TypeScript.",
    memory: {
      title: "Dlouhodobá sdílená paměť",
      detail:
        "Ukládejte rozhodnutí, omezení a poznatky pro další relace. Tým i jeho agenti si je mohou prohlížet a upravovat na jednom místě.",
      link: "Paměť pro programovací agenty (anglicky)",
    },
    documents: {
      title: "Vyhledávání v dokumentech s citacemi",
      detail:
        "Najděte interní pravidlo, provozní postup nebo technický podklad. Otevřete citovanou pasáž v příslušné verzi dokumentu.",
      link: "Vyhledávání v dokumentech a RAG (anglicky)",
    },
    code: {
      title: "Orientace ve zdrojovém kódu",
      detail:
        "Hledejte symboly, sledujte volání funkcí a propojte poznatky s konkrétním kódem. Loom ukáže, zda se kód od uložení poznatku změnil.",
      link: "Paměť propojená s kódem (anglicky)",
    },
    useCasesHeading: "Předávejte poznatky mezi agenty",
    useCasesDetail:
      "Zpřístupněte dalšímu agentovi rozhodnutí z implementace, zjištění z kontroly kódu a provozní zkušenosti.",
    useCasesLabel: "Využití paměti pro AI agenty",
    softwareFactory: "Paměť pro softwarovou továrnu",
    knowledgeFactory: "Znalostní továrna pro AI agenty",
    autonomous: "Paměť pro autonomní agenty",
    selfHosted: "Paměť agentů na vlastním serveru",
    evaluationHeading: "Ověřte Loom na práci svého týmu",
    evaluationDetail:
      "Na konkrétní úloze ověřte, co agent dohledá, na jaké zdroje odkáže a kdo má k uloženým údajům přístup.",
    evaluate: "Vyzkoušet na vlastní úloze (anglicky)",
    deploymentOptions: "Možnosti nasazení (anglicky)",
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
    knowledgeFactory: "Evaluar una fábrica de conocimiento para agentes de IA",
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
  hi: {
    title: "एआई एजेंटों के लिए साझा मेमोरी | ClickHouse Loom",
    description: "एआई एजेंटों के लिए ओपन सोर्स मेमोरी। सत्रों के बीच फ़ैसले सहेजें, स्रोतों के साथ दस्तावेज़ खोजें और सीख को कोड से जोड़ें।",
    deployment: "ओपन सोर्स · अपने ClickHouse इंस्टेंस पर चलाएँ",
    heading: "एआई एजेंटों के लिए साझा मेमोरी",
    lead: "Loom टीम के फ़ैसलों और एजेंटों की सीख को सत्रों के बीच सहेजता है। एजेंट सहेजी गई जानकारी खोज सकते हैं, उद्धृत दस्तावेज़ जाँच सकते हैं और संबंधित कोड खोल सकते हैं।",
    run: "Loom इंस्टॉल करें",
    product: "उत्पाद के बारे में जानें",
    connect: "कोडिंग एजेंट जोड़ें",
    sdk: "SDK से बनाएँ",
    screenshotAlt: "Loom डैशबोर्ड में भुगतान से जुड़ा एक फ़ैसला, संबंधित फ़ंक्शन और कोड में बदलाव की सूचना",
    screenshotCaption: "सहेजा गया फ़ैसला उस फ़ंक्शन की ओर इशारा करता है जिस पर वह लागू होता है।",
    contextHeading: "अगले जवाब के लिए ज़रूरी संदर्भ",
    contextDetail: "एजेंट इस जानकारी तक MCP, HTTP या Python और TypeScript के SDK से पहुँच सकते हैं।",
    memory: {
      title: "साझा मेमोरी",
      detail: "फ़ैसले, सीमाएँ और सीखी गई बातें अगले सत्रों के लिए सहेजें। टीम और उसके एजेंट एक ही जगह उन्हें देख और अपडेट कर सकते हैं।",
      link: "कोडिंग एजेंटों के लिए मेमोरी",
    },
    documents: {
      title: "स्रोतों के साथ दस्तावेज़ खोजें",
      detail: "सवाल का जवाब देने वाली नीति, प्रक्रिया या संदर्भ खोजें। दस्तावेज़ के इस्तेमाल किए गए संस्करण में उद्धृत अंश खोलें।",
      link: "दस्तावेज़ खोज और RAG",
    },
    code: {
      title: "सोर्स कोड का विश्लेषण",
      detail:
        "सिंबल खोजें, फ़ंक्शन कॉल का पता लगाएँ और एजेंटों की सीख को कोड से जोड़ें। Loom दिखाता है कि जानकारी सहेजने के बाद कोड बदला है या नहीं।",
      link: "मेमोरी को सोर्स कोड से जोड़ें",
    },
    useCasesHeading: "एजेंटों की सीख साझा करें",
    useCasesDetail: "डेवलपमेंट के फ़ैसले, कोड रिव्यू के निष्कर्ष और संचालन की जानकारी अगले एजेंट के लिए उपलब्ध रखें।",
    useCasesLabel: "एआई एजेंट मेमोरी के उपयोग",
    softwareFactory: "सॉफ़्टवेयर फ़ैक्टरी के लिए मेमोरी",
    knowledgeFactory: "एआई एजेंटों के लिए नॉलेज फ़ैक्टरी",
    autonomous: "स्वायत्त एजेंटों के लिए मेमोरी",
    selfHosted: "सेल्फ़-होस्टेड एजेंट मेमोरी",
    evaluationHeading: "अपनी टीम के लिए Loom का मूल्यांकन करें",
    evaluationDetail: "टीम के किसी काम पर जानकारी की खोज, स्रोतों के लिंक और पहुँच की अनुमतियाँ जाँचें।",
    evaluate: "Loom आज़माएँ",
    deploymentOptions: "डिप्लॉयमेंट के विकल्प",
  },
  id: {
    title: "Memori agen AI dan pencarian kode | ClickHouse Loom",
    description:
      "Memori bersama untuk agen pemrograman. Gunakan kembali keputusan dari sesi lain, cari dokumen dengan kutipan, dan telusuri kode terkait.",
    deployment: "Sumber terbuka · Dihosting sendiri dengan ClickHouse",
    heading: "Memori bersama untuk agen AI",
    lead: "Loom menyimpan keputusan dan pelajaran agar dapat digunakan kembali di sesi lain. Agen dapat menemukan keputusan tim, membaca sumber yang dikutip, dan melihat kode yang terkait.",
    run: "Jalankan Loom",
    product: "Lihat fitur Loom",
    connect: "Hubungkan agen pemrograman",
    sdk: "Gunakan SDK",
    screenshotAlt:
      "Dasbor Loom menampilkan keputusan tentang pembayaran yang ditautkan ke sebuah fungsi dan peringatan bahwa kode terkait telah berubah",
    screenshotCaption: "Keputusan yang tersimpan tertaut ke kode yang dirujuknya.",
    contextHeading: "Konteks untuk sesi berikutnya",
    contextDetail: "Akses ketiga jenis konteks ini lewat MCP, HTTP, atau SDK Python dan TypeScript.",
    memory: {
      title: "Memori bersama",
      detail:
        "Simpan keputusan, aturan, dan pelajaran dari satu sesi untuk digunakan pada sesi berikutnya. Tim dan agen dapat meninjau serta memperbaruinya di tempat yang sama.",
      link: "Memori untuk agen pemrograman",
    },
    documents: {
      title: "Pencarian dokumen",
      detail: "Cari kebijakan, panduan operasional, atau referensi. Buka kutipan beserta revisi dokumen sumbernya.",
      link: "Pencarian dokumen dan RAG",
    },
    code: {
      title: "Penelusuran kode",
      detail:
        "Temukan simbol dan kode yang memanggilnya. Tautkan memori ke kode, lalu periksa apakah sumber yang dirujuk sudah berubah.",
      link: "Memori yang tertaut ke kode",
    },
    useCasesHeading: "Kapan menggunakan Loom",
    useCasesDetail: "Simpan pengetahuan proyek saat beberapa agen mengerjakan repositori atau dokumen yang sama.",
    useCasesLabel: "Kasus penggunaan Loom",
    softwareFactory: "Memori untuk software factory",
    knowledgeFactory: "Pabrik pengetahuan untuk agen AI",
    autonomous: "Memori jangka panjang untuk agen otonom",
    selfHosted: "Memori agen yang dihosting sendiri",
    evaluationHeading: "Uji Loom pada repositori Anda",
    evaluationDetail:
      "Gunakan pekerjaan sehari-hari untuk menguji pencarian kode, pengambilan kembali memori lintas sesi, dan izin akses sebelum memperluas penggunaan.",
    evaluate: "Evaluasi Loom",
    deploymentOptions: "Opsi penerapan",
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
  pl: {
    title: "Współdzielona pamięć dla agentów AI | ClickHouse Loom",
    description:
      "Długoterminowa pamięć dla agentów programistycznych. Uruchom Loom we własnym środowisku ClickHouse, przeszukuj dokumenty i wiąż ustalenia z kodem.",
    deployment: "Otwarty kod źródłowy · Wdrożenie na własnej infrastrukturze",
    heading: "Wspólna pamięć dla agentów AI",
    lead: "Loom zachowuje decyzje i ustalenia zespołu między sesjami. Agenci mogą wrócić do wcześniejszych ustaleń, sprawdzić cytowane dokumenty i otworzyć powiązany kod źródłowy.",
    run: "Zainstaluj Loom (po angielsku)",
    product: "Poznaj funkcje (po angielsku)",
    connect: "Połącz agentów programistycznych (po angielsku)",
    sdk: "Twórz z użyciem SDK (po angielsku)",
    screenshotAlt: "Panel Loom z decyzją dotyczącą płatności, odnośnikiem do funkcji i ostrzeżeniem o zmianie kodu",
    screenshotCaption: "Zapisana decyzja wskazuje funkcję, której dotyczy.",
    contextHeading: "Źródła dla kolejnej odpowiedzi",
    contextDetail: "Agenci korzystają z pamięci, dokumentów i kodu przez MCP, HTTP lub SDK dla Pythona i TypeScriptu.",
    memory: {
      title: "Długoterminowa pamięć zespołu",
      detail:
        "Zachowuj decyzje, ograniczenia i ustalenia na kolejne sesje. Zespół i jego agenci mogą je przeglądać i aktualizować w jednym miejscu.",
      link: "Pamięć dla agentów programistycznych (po angielsku)",
    },
    documents: {
      title: "Wyszukiwanie w dokumentach z cytatami",
      detail:
        "Znajdź wewnętrzną zasadę, instrukcję operacyjną lub opis techniczny. Otwórz cytowany fragment we właściwej wersji dokumentu.",
      link: "Wyszukiwanie w dokumentach i RAG (po angielsku)",
    },
    code: {
      title: "Wyszukiwanie i analiza kodu",
      detail:
        "Znajdź definicje i miejsca wywołań, a ustalenia powiąż z konkretnym kodem. Loom pokaże, czy kod zmienił się od zapisania wpisu.",
      link: "Pamięć powiązana z kodem (po angielsku)",
    },
    useCasesHeading: "Przekazuj ustalenia między agentami",
    useCasesDetail:
      "Udostępnij kolejnemu agentowi decyzje z prac nad kodem, wyniki przeglądu i wskazówki z eksploatacji.",
    useCasesLabel: "Zastosowania pamięci dla agentów AI",
    softwareFactory: "Pamięć dla fabryki oprogramowania",
    knowledgeFactory: "Fabryka wiedzy dla agentów AI",
    autonomous: "Pamięć dla autonomicznych agentów",
    selfHosted: "Pamięć agentów na własnej infrastrukturze",
    evaluationHeading: "Sprawdź Loom w pracy swojego zespołu",
    evaluationDetail:
      "Wybierz rzeczywiste zadanie. Sprawdź, co agent odnajdzie, na jakie źródła się powoła i kto ma dostęp do zapisanych danych.",
    evaluate: "Sprawdź na własnym zadaniu (po angielsku)",
    deploymentOptions: "Opcje wdrożenia (po angielsku)",
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
  tr: {
    title: "Yapay zekâ ajan belleği ve kod arama | ClickHouse Loom",
    description:
      "Kodlama ajanları için ortak bellek. Oturumlar arasında kararları hatırlayın, alıntı yapılan belgeleri bulun ve ilgili kodun izini sürün.",
    deployment: "Açık kaynak · ClickHouse ile kendi altyapınızda",
    heading: "Yapay zekâ ajanları için ortak bellek",
    lead: "Loom, ekip kararlarını ve edinilen bilgileri oturumlar arasında saklar. Ajanlar önceki kararları bulabilir, alıntıların kaynağını açabilir ve ilgili kodu inceleyebilir.",
    run: "Loom’u çalıştırın",
    product: "Ürünü inceleyin",
    connect: "Kodlama ajanlarını bağlayın",
    sdk: "SDK’ları kullanın",
    screenshotAlt: "Loom panosunda bir işleve bağlanan ödeme kararı ve ilgili kodun değiştiğini bildiren uyarı",
    screenshotCaption: "Kaydedilen karar, ilgili koda bağlanır.",
    contextHeading: "Bir sonraki oturum için bağlam",
    contextDetail: "Ajanlar bu üç bağlam türüne MCP, HTTP veya Python ve TypeScript SDK’ları üzerinden ulaşabilir.",
    memory: {
      title: "Ortak bellek",
      detail:
        "Kararları, kuralları ve öğrenilenleri sonraki oturumlara taşıyın. Ekipler ve ajanlar bunları ortak bir yerde inceleyip güncelleyebilir.",
      link: "Kodlama ajanları için bellek",
    },
    documents: {
      title: "Belge arama",
      detail:
        "Sorunuzu yanıtlayan politikayı, işletim kılavuzunu veya başvuru belgesini bulun. Alıntıyı ve alındığı belge sürümünü açın.",
      link: "Belge arama ve RAG",
    },
    code: {
      title: "Kod analizi",
      detail:
        "Kod sembollerini ve onları çağıran yerleri bulun. Bellek kayıtlarını kodla ilişkilendirin ve kaynak kod değiştiğinde bunu görün.",
      link: "Kodla ilişkilendirilmiş bellek",
    },
    useCasesHeading: "Loom nerede kullanılır?",
    useCasesDetail:
      "Aynı kod deposunda veya belgeler üzerinde çalışan ajanların proje bilgisini ortak bir yerde tutun.",
    useCasesLabel: "Loom kullanım alanları",
    softwareFactory: "Yazılım fabrikaları için bellek",
    knowledgeFactory: "Yapay zekâ ajanları için bilgi fabrikası",
    autonomous: "Otonom ajanlar için kalıcı bellek",
    selfHosted: "Kendi altyapınızda ajan belleği",
    evaluationHeading: "Loom’u kod deponuzda deneyin",
    evaluationDetail:
      "Kullanımı genişletmeden önce günlük işlerde kod aramayı, oturumlar arası bellek erişimini ve erişim izinlerini sınayın.",
    evaluate: "Loom’u değerlendirin",
    deploymentOptions: "Kurulum seçenekleri",
  },
  vi: {
    title: "Bộ nhớ dùng chung cho tác nhân AI | ClickHouse Loom",
    description:
      "Bộ nhớ cho tác nhân lập trình AI. Dùng lại quyết định qua nhiều phiên, tìm tài liệu kèm trích dẫn và truy vết mã nguồn.",
    deployment: "Mã nguồn mở · Tự triển khai trên ClickHouse",
    heading: "Bộ nhớ dùng chung cho tác nhân AI",
    lead: "Loom lưu quyết định và bài học của nhóm để tác nhân AI dùng lại ở phiên sau. Tác nhân có thể kiểm tra đoạn tài liệu được trích dẫn và mở mã nguồn liên quan.",
    run: "Cài đặt Loom",
    product: "Khám phá Loom",
    connect: "Kết nối tác nhân lập trình",
    sdk: "Phát triển với SDK",
    screenshotAlt:
      "Bảng điều khiển Loom hiển thị quyết định về thanh toán, hàm liên quan và cảnh báo khi mã nguồn thay đổi",
    screenshotCaption: "Mỗi quyết định đã lưu có thể liên kết đến đoạn mã liên quan.",
    contextHeading: "Ngữ cảnh cho phiên làm việc tiếp theo",
    contextDetail:
      "Tác nhân có thể truy xuất bộ nhớ, tài liệu và mã nguồn qua MCP, HTTP hoặc SDK Python và TypeScript.",
    memory: {
      title: "Bộ nhớ dùng chung",
      detail:
        "Tác nhân viết mã lưu quyết định của dự án. Tác nhân rà soát có thể đọc lại quyết định đó trong một phiên khác nếu được cấp quyền.",
      link: "Bộ nhớ cho tác nhân lập trình",
    },
    documents: {
      title: "Tìm kiếm tài liệu có trích dẫn",
      detail:
        "Tìm chính sách, hướng dẫn vận hành hoặc tài liệu tham khảo. Mở đoạn được trích dẫn và xem phiên bản của tài liệu nguồn.",
      link: "Tìm kiếm tài liệu và RAG",
    },
    code: {
      title: "Tra cứu mã nguồn",
      detail:
        "Tìm hàm, lớp và nơi gọi chúng. Liên kết bài học với mã nguồn để biết khi nào đoạn mã được tham chiếu đã thay đổi.",
      link: "Bộ nhớ liên kết với mã nguồn",
    },
    useCasesHeading: "Chia sẻ kiến thức giữa các tác nhân",
    useCasesDetail:
      "Giữ lại quyết định triển khai, nhận xét khi rà soát mã và kinh nghiệm vận hành để tác nhân tiếp theo dùng lại.",
    useCasesLabel: "Các trường hợp sử dụng Loom",
    softwareFactory: "Bộ nhớ cho nhà máy phần mềm",
    knowledgeFactory: "Nhà máy tri thức cho tác nhân AI",
    autonomous: "Bộ nhớ dài hạn cho tác nhân tự trị",
    selfHosted: "Tự triển khai bộ nhớ cho tác nhân AI",
    evaluationHeading: "Thử Loom trên kho mã của nhóm",
    evaluationDetail:
      "Dùng công việc thực tế để kiểm tra khả năng truy xuất bộ nhớ giữa các phiên, liên kết đến nguồn và quyền truy cập.",
    evaluate: "Đánh giá Loom",
    deploymentOptions: "Các phương án triển khai",
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
    knowledgeFactory: "评估 AI 智能体知识工厂",
    autonomous: "自主智能体的记忆",
    selfHosted: "自托管智能体记忆",
    evaluationHeading: "为你的团队评估 Loom",
    evaluationDetail: "用团队的实际工作负载验证记忆召回、来源链接和访问控制。",
    evaluate: "开始评估",
    deploymentOptions: "查看部署选项",
  },
  "zh-Hant": {
    title: "AI 代理的共用記憶與程式碼搜尋 | ClickHouse Loom",
    description:
      "讓程式開發代理跨工作階段共用長期記憶。用 MCP 取回團隊決策、搜尋文件、核對引用段落，並追蹤相關原始碼的變更。",
    deployment: "開放原始碼 · 可在 ClickHouse 上自架",
    heading: "AI 代理的共用記憶",
    lead: "Loom 保留團隊的決策與經驗，讓代理在下一個工作階段接著做。代理可以找回先前的決策、查閱引用的文件段落，並開啟相關原始碼。",
    run: "安裝 Loom（英文）",
    product: "認識 Loom（英文）",
    connect: "連接程式開發代理（英文）",
    sdk: "使用 SDK 開發（英文）",
    screenshotAlt: "Loom 控制台顯示一項連結至函式的付款決策，並提醒相關程式碼已變更",
    screenshotCaption: "儲存的決策可以連到適用的原始碼。",
    contextHeading: "為下一個工作階段保留脈絡",
    contextDetail: "代理可以透過 MCP、HTTP 或 Python 與 TypeScript SDK 查詢記憶、文件和程式碼。",
    memory: {
      title: "共用長期記憶",
      detail: "程式開發代理儲存專案決策。審查代理取得同一命名空間的讀取權限後，可以在另一個工作階段取回同一筆決策。",
      link: "程式開發代理的記憶（英文）",
    },
    documents: {
      title: "附有引文的文件搜尋",
      detail: "查找政策、操作手冊與參考資料。開啟引用的段落並核對原始文件的版本。",
      link: "文件搜尋與 RAG（英文）",
    },
    code: {
      title: "程式碼搜尋",
      detail: "查找函式、類別和呼叫位置。將經驗連結到相關原始碼，查看程式碼是否在記錄後變更。",
      link: "連結原始碼的記憶（英文）",
    },
    useCasesHeading: "讓接手的代理用得到團隊經驗",
    useCasesDetail: "保留實作決策、程式碼審查結果和維運經驗，讓其他代理接續使用。",
    useCasesLabel: "Loom 使用案例",
    softwareFactory: "軟體工廠的共用記憶",
    knowledgeFactory: "為 AI 代理建立知識工廠",
    autonomous: "自主代理的長期記憶",
    selfHosted: "自架 AI 代理記憶服務",
    evaluationHeading: "用團隊的程式碼儲存庫評估 Loom",
    evaluationDetail: "用實際工作驗證跨工作階段的記憶查詢、來源連結和存取權限。",
    evaluate: "開始評估（英文）",
    deploymentOptions: "查看部署方式（英文）",
  },
};
