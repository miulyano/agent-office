export type Agent = {
  id: string
  type: string
  desc: string
  isWorking: boolean
  activity: string
  startedAt: number
  endedAt: number
  calls: number
  cost: number
  model: string
  tool: string
  at: number
  finished: boolean
  session: number   // номер сессии задачи: 1 — корень
  // an epic role: what its Claude Code job file says (working, idle, done, stopped)
  job?: string
}

export type LogLine = { at: number; who: string; text: string }

export type Producer = { isBusy: boolean; activity: string; at: number; calls: number; cost: number; model: string; tool: string; waitingFor?: string; owner?: boolean }
// руководитель одной сессии задачи; первый — корень, остальные порождены приложением
export type Head = Producer & { id: string; title: string; isLive: boolean; branch: string; since: number }

// фоновая команда руководителя; status из уведомления: completed, failed, killed
export type Shell = { id: string; name: string; session: number; status: string; start: number; end: number }

export type Studio = {
  agents: Agent[]
  producer: Producer
  sessions: Head[]
  cost: number
  log: LogLine[]
  since: number
  worked?: number   // мс, когда работал хоть кто-то: руководители, агенты, фоновые команды
  workingSince?: number   // начало идущего отрезка, он в worked не входит; 0 — сейчас никто не работает
  office: Office
  shells?: Shell[]
}

export type OfficeDesk = { type: string; name: string; short?: string; prop: string; shirt: string; hair: string; skin: number; locked?: boolean }

export type Office = {
  folder: string
  title: string
  // note: the plaque's second line instead of the head count; strip: a floor folded to a band, nobody at work there;
  // track: an epic floor's stages left to right, each [label, passed | next | rejected | ahead]
  zones: Array<{ id?: string; name: string; desks: OfficeDesk[]; note?: string; strip?: boolean; track?: Array<[string, string]> | null }>
  guests: Record<string, { name: string; prop: string }>
  saved?: boolean
  lang?: string
  hidden?: number
  generating?: boolean
  provider?: string
  accent?: string
  // an orchestrating-epics epic: the coordinator's context against its compaction line
  epic?: { context: number | null; limit: number | null }
}
