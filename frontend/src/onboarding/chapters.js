// I capitoli del tutorial del primo accesso, uno per pagina. Il tour non
// guida l'utente da una pagina all'altra: lo ACCOMPAGNA dove va. Quando entra
// in una route con un capitolo non ancora visto, il capitolo parte; se cambia
// pagina a metà si chiude senza lasciare tracce e ricomincerà la prossima
// volta che passa di lì. Così "clicca qui" non è mai un vicolo cieco: chi
// clicca altrove vede semplicemente il capitolo dell'altra pagina.
//
// Ogni capitolo è una FUNZIONE del contesto (utente, policy dell'admin, t):
// gli step che non hanno senso per questo utente o questa installazione non
// vengono nemmeno prodotti. Gli elementi si trovano con `data-tour="…"`, e
// non con classi o struttura del DOM: quando un componente non è renderizzato
// il suo attributo sparisce con lui, e il provider salta lo step (o cambia
// testo, vedi `mode` qui sotto) senza bisogno di sapere perché.

export const CHAPTERS = [
  {
    key: 'new',
    match: (path) => path === '/new',
    steps: ({ t, anonRequired }) => [
      // intro senza bersaglio, a centro schermo: logo, titolo e una riga.
      // driver.js accetta HTML nella descrizione; il markup sta qui e non nel
      // catalogo, dove ci sono solo i testi
      { popoverClass: 'blockingbear-tour blockingbear-tour-welcome',
        description:
          '<img src="/logo.png" alt="" class="blockingbear-tour-logo" draggable="false">' +
          `<h2 class="blockingbear-tour-welcome-title">${t('new.welcome.title')}</h2>` +
          `<p>${t('new.welcome.body')}</p>` },
      { element: '[data-tour="nav"]', side: 'right', align: 'start',
        title: t('new.nav.title'), description: t('new.nav.body') },
      { element: '[data-tour="new-anon"]', side: 'bottom',
        title: t('new.mode.title'),
        // con la policy obbligatoria la card "in chiaro" è visibile ma
        // disabilitata: si spiega il perché, invece di far cercare all'utente
        // un pulsante che non può premere
        description: t(anonRequired ? 'new.mode.bodyRequired' : 'new.mode.body') },
      { element: '[data-tour="user-menu"]', side: 'top', align: 'start',
        title: t('new.menu.title'), description: t('new.menu.body') },
    ],
  },
  {
    key: 'chat',
    match: (path) => /^\/chats\/[^/]+$/.test(path) || /^\/projects\/[^/]+\/chat$/.test(path),
    steps: ({ t }) => [
      { element: '[data-tour="model"]', side: 'bottom', align: 'start',
        title: t('chat.model.title'), description: t('chat.model.body') },
      { element: '[data-tour="options"]', side: 'bottom', align: 'start',
        title: t('chat.options.title'), description: t('chat.options.body') },
      // solo nelle chat anonimizzate libere: in chiaro (e nelle chat di
      // progetto) il componente non è renderizzato e lo step viene saltato
      { element: '[data-tour="anon-tags"]', side: 'bottom', align: 'start',
        title: t('chat.anonTags.title'), description: t('chat.anonTags.body') },
      // anche questa solo nelle chat anonimizzate: sta nel banner verde
      { element: '[data-tour="review"]', side: 'bottom', align: 'end',
        title: t('chat.review.title'), description: t('chat.review.body') },
      { element: '[data-tour="composer"]', side: 'top',
        title: t('chat.composer.title'), description: t('chat.composer.body') },
    ],
  },
  {
    key: 'project',
    match: (path) => /^\/projects\/[^/]+$/.test(path),
    steps: ({ t }) => [
      // la dropzone dice da sé se il progetto è anonimizzato (data-tour
      // diverso): esiste solo una delle due varianti, l'altra viene saltata
      { element: '[data-tour="dropzone-anon"]', side: 'bottom',
        title: t('project.drop.title'), description: t('project.drop.bodyAnon') },
      { element: '[data-tour="dropzone-plain"]', side: 'bottom',
        title: t('project.drop.title'), description: t('project.drop.bodyPlain') },
      { element: '[data-tour="project-new-chat"]', side: 'bottom', align: 'end',
        title: t('project.newChat.title'), description: t('project.newChat.body') },
    ],
  },
  {
    key: 'projects',
    match: (path) => path === '/projects',
    steps: ({ t }) => [
      { element: '[data-tour="new-project"]', side: 'bottom', align: 'end',
        title: t('projects.new.title'), description: t('projects.new.body') },
    ],
  },
]

export function chapterFor(pathname) {
  return CHAPTERS.find((c) => c.match(pathname)) ?? null
}
