// The status card: one concept per person at self/now.md, published public.
// Everything here is pure so the tests can hold it without an engine.

import type { Card, Conversation, Contacts, Message, Pair } from '../types'

export const CARD_PATH = 'self/now.md'

const MAX_TOPICS = 6
const MAX_TOPIC_CHARS = 32
const MAX_WORKING_ON_CHARS = 200

export function cleanTopic(raw: string): string {
  return raw
    .replace(/[\r\n\t]+/g, ' ')
    .replace(/^[#\s,]+|[\s,]+$/g, '')
    .toLowerCase()
    .slice(0, MAX_TOPIC_CHARS)
    .trim()
}

export function cleanTopics(raw: readonly string[]): string[] {
  const out: string[] = []
  for (const one of raw) {
    const topic = cleanTopic(one)
    if (topic && !out.includes(topic)) out.push(topic)
    if (out.length >= MAX_TOPICS) break
  }
  return out
}

export function cleanWorkingOn(raw: string): string {
  return raw.replace(/\s+/g, ' ').trim().slice(0, MAX_WORKING_ON_CHARS)
}

// Every value goes through JSON.stringify: a JSON string is a valid YAML
// double-quoted scalar, so a colon or a quote in what someone typed can never
// break the frontmatter.
export function cardMarkdown(card: Card): string {
  const q = (v: unknown) => JSON.stringify(v)
  const topics = card.topics.length ? card.topics.join(' · ') : '(none)'
  return [
    '---',
    'type: status',
    `title: ${q(`Now: @${card.handle}`)}`,
    `description: ${q(card.workingOn)}`,
    `space: ${q(card.space)}`,
    `working_on: ${q(card.workingOn)}`,
    `topics: ${q(card.topics)}`,
    `updated: ${q(card.updated)}`,
    `timestamp: ${q(card.updated)}`,
    'tags:',
    '- status',
    `- ${q(`space-${card.space}`)}`,
    '---',
    '',
    '# Now',
    '',
    `**Working on:** ${card.workingOn}`,
    '',
    `**Topics:** ${topics}`,
    '',
    `Published from the People pane in Claude Code, space \`${card.space}\`.`,
    '',
  ].join('\n')
}

function frontmatter(content: string): Record<string, unknown> {
  const match = /^---\r?\n([\s\S]*?)\r?\n---/.exec(content)
  if (!match) return {}
  const out: Record<string, unknown> = {}
  for (const line of match[1]!.split(/\r?\n/)) {
    const kv = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(line)
    if (!kv) continue
    const raw = kv[2]!.trim()
    try {
      out[kv[1]!] = JSON.parse(raw)
    } catch {
      out[kv[1]!] = raw.replace(/^'(.*)'$/, '$1')
    }
  }
  return out
}

export function parseCard(handle: string, content: string): Card | null {
  const fm = frontmatter(content)
  if (fm.type !== 'status') return null
  const workingOn = typeof fm.working_on === 'string' ? fm.working_on : ''
  const topics = Array.isArray(fm.topics) ? fm.topics.filter((t): t is string => typeof t === 'string') : []
  return {
    handle,
    workingOn: cleanWorkingOn(workingOn),
    topics: cleanTopics(topics),
    space: typeof fm.space === 'string' ? fm.space : '',
    updated: typeof fm.updated === 'string' ? fm.updated : '',
  }
}

export function sharedTopics(a: readonly string[], b: readonly string[]): string[] {
  return a.filter(topic => b.includes(topic))
}

// A topic-suggestion reply is model text: take the first JSON array of
// strings in it and nothing else.
export function parseTopicReply(text: string): string[] {
  const match = /\[[\s\S]*?\]/.exec(text)
  if (!match) return []
  try {
    const value: unknown = JSON.parse(match[0])
    return Array.isArray(value) ? cleanTopics(value.filter((t): t is string => typeof t === 'string')) : []
  } catch {
    return []
  }
}

// --- Shapes the engram tools answer, read defensively ------------------

const str = (v: unknown) => (typeof v === 'string' ? v : '')
const strs = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])
const rec = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' ? (v as Record<string, unknown>) : {})

export function readConversations(value: unknown): Conversation[] {
  const list = rec(value).conversations
  if (!Array.isArray(list)) return []
  return list
    .map(rec)
    .filter(c => typeof c.with === 'string')
    .map(c => ({ with: str(c.with), unread: Number(c.unread) || 0, last: str(c.last) }))
}

export function readMessages(value: unknown): Message[] {
  const list = rec(value).messages
  if (!Array.isArray(list)) return []
  return list.map(rec).map(m => ({ from: str(m.from), body: str(m.body), at: str(m.at) }))
}

export function readContacts(value: unknown): Contacts {
  const v = rec(value)
  return { contacts: strs(v.contacts), incoming: strs(v.incoming), outgoing: strs(v.outgoing) }
}

export function readPairs(value: unknown): Pair[] {
  const list = rec(value).pairs
  if (!Array.isArray(list)) return []
  return list
    .map(rec)
    .map(p => ({ mine: str(rec(p.mine).title) || str(rec(p.mine).path), theirs: str(rec(p.theirs).title) || str(rec(p.theirs).path) }))
    .filter(p => p.mine && p.theirs)
}

export function ago(minutes: number | null, updated: string, now: number): string {
  const m = minutes ?? (updated ? Math.round((now - Date.parse(updated)) / 60000) : NaN)
  if (!Number.isFinite(m)) return ''
  if (m < 1) return 'active now'
  if (m < 60) return `${m}m ago`
  if (m < 60 * 24) return `${Math.round(m / 60)}h ago`
  return `${Math.round(m / 1440)}d ago`
}
