export type View = 'space' | 'checkin' | 'chat'

export type Card = {
  handle: string
  workingOn: string
  topics: string[]
  space: string
  updated: string
}

export type Person = {
  handle: string
  displayName: string
  card: Card
  minutesAgo: number | null
}

export type Pair = { mine: string; theirs: string }

export type Overlap = {
  shared: string[]
  pairs: Pair[]
  isLoaded: boolean
}

export type Contacts = { contacts: string[]; incoming: string[]; outgoing: string[] }

export type Conversation = { with: string; unread: number; last: string }

export type Message = { from: string; body: string; at: string }

export type Chat = { with: string; messages: Message[] }

export type Draft = {
  workingOn: string
  topics: string[]
  chosen: string[]
  isSuggesting: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'engram-people': {
      view: View
      me: string
      myCard: Card | null
      people: Person[]
      overlap: Record<string, Overlap>
      contacts: Contacts
      conversations: Conversation[]
      chat: Chat | null
      draft: Draft
      note: string
      busy: string
    }
  }
}
