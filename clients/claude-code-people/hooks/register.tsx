// People pane: a social window inside Claude Code, built only on the Engram
// tools the person's own connector already exposes. No model turn is ever
// started for a human DM: a clock polls kb_messages and redraws.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Card, Chat, Contacts, Conversation, Draft, Overlap, Person, View } from '../types'
import {
  CARD_PATH,
  ago,
  cardMarkdown,
  cleanTopic,
  cleanTopics,
  cleanWorkingOn,
  parseCard,
  parseTopicReply,
  readContacts,
  readConversations,
  readMessages,
  readPairs,
  sharedTopics,
} from './card'

const PANE = 'people'
const POLL_MS = 5000
const CONTACTS_EVERY = 4 // polls

const view = atom({ plugin: 'engram-people', key: 'view' } as const, 'space' as View)
const me = atom({ plugin: 'engram-people', key: 'me' } as const, '')
const myCard = atom({ plugin: 'engram-people', key: 'myCard' } as const, null as Card | null)
const people = atom({ plugin: 'engram-people', key: 'people' } as const, [] as Person[])
const overlap = atom({ plugin: 'engram-people', key: 'overlap' } as const, {} as Record<string, Overlap>)
const contacts = atom({ plugin: 'engram-people', key: 'contacts' } as const, {
  contacts: [],
  incoming: [],
  outgoing: [],
} as Contacts)
const conversations = atom({ plugin: 'engram-people', key: 'conversations' } as const, [] as Conversation[])
const chat = atom({ plugin: 'engram-people', key: 'chat' } as const, null as Chat | null)
const draft = atom({ plugin: 'engram-people', key: 'draft' } as const, {
  workingOn: '',
  topics: [],
  chosen: [],
  isSuggesting: false,
} as Draft)
const note = atom({ plugin: 'engram-people', key: 'note' } as const, '')
const busy = atom({ plugin: 'engram-people', key: 'busy' } as const, '')

type $ = EngineInterface


// Set by register from the plugin's options; module variables start over on
// every reload, which is what the poll bookkeeping wants too.
let server = 'claude.ai engram'
let space = 'vibechk'
let isPolling = false
let pollCount = 0
let seenUnread: Map<string, number> | null = null
let seenIncoming: Set<string> | null = null

async function engram($: $, tool: string, args: Record<string, unknown> = {}): Promise<unknown> {
  const result = await $.mcp.call(server, tool, args)
  const text = result.content
    .filter(block => block.type === 'text')
    .map(block => block.text ?? '')
    .join('')
  if (result.isError) throw new Error(text.replace(/^Error executing tool [\w-]+:\s*/, '') || `${tool} failed`)
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function fail($: $, what: string, err: unknown) {
  return update($, note, () => `${what}: ${err instanceof Error ? err.message : String(err)}`.slice(0, 300))
}

// --- Loading the space ---------------------------------------------

async function loadSpace($: $, isFirst = false) {
  await update($, busy, () => 'Loading the space…')
  await update($, note, () => '')
  try {
    const [team, explore, graph] = await Promise.all([
      engram($, 'kb_team'),
      engram($, 'kb_explore'),
      engram($, 'kb_contacts'),
    ])
    const t = (team ?? {}) as { me?: { handle?: string }; team?: { handle?: string; minutes_ago?: number }[] }
    const handle = t.me?.handle ?? ''
    await update($, me, () => handle)
    const minutes = new Map((t.team ?? []).map(row => [row.handle ?? '', row.minutes_ago ?? null]))
    const directory = ((explore as { people?: { handle?: string; display_name?: string }[] })?.people ?? []).filter(
      p => p.handle && p.handle !== handle,
    )
    const c = readContacts(graph)
    await update($, contacts, () => c)
    seenIncoming ??= new Set(c.incoming)

    const names = new Map(directory.map(p => [p.handle!, p.display_name || p.handle!]))
    for (const h of [...c.contacts, ...c.incoming, ...c.outgoing]) if (!names.has(h)) names.set(h, h)

    const mine = await engram($, 'kb_read', { path: CARD_PATH })
      .then(r => parseCard(handle, String((r as { content?: string }).content ?? '')))
      .catch(() => null)
    await update($, myCard, () => mine)

    const found = await Promise.all(
      [...names].map(([h, displayName]) =>
        engram($, 'kb_read_public', { handle: h, path: CARD_PATH })
          .then(r => parseCard(h, String((r as { content?: string }).content ?? '')))
          .then(card => (card ? { handle: h, displayName, card, minutesAgo: minutes.get(h) ?? null } : null))
          .catch(() => null),
      ),
    )
    const members = found.filter((p): p is Person => p !== null && p.card.space === space)
    await update($, people, () => members)
    if (isFirst && !mine) await update($, view, () => 'checkin' as View)
    void loadOverlap($, mine, members)
  } catch (err) {
    await fail($, `Could not reach ${server}`, err)
  } finally {
    await update($, busy, () => '')
  }
}

async function loadOverlap($: $, mine: Card | null, members: Person[]) {
  const start: Record<string, Overlap> = {}
  for (const p of members) start[p.handle] = { shared: sharedTopics(mine?.topics ?? [], p.card.topics), pairs: [], isLoaded: false }
  await update($, overlap, () => start)
  await Promise.all(
    members.map(p =>
      engram($, 'kb_common_ground', { handle: p.handle })
        .then(r => readPairs(r).slice(0, 3))
        .catch(() => [])
        .then(pairs =>
          update($, overlap, all => ({ ...all, [p.handle]: { ...(all[p.handle] ?? start[p.handle]!), pairs, isLoaded: true } })),
        ),
    ),
  )
}

// --- Check in ------------------------------------------------------

async function suggestTopics($: $) {
  await update($, draft, d => ({ ...d, isSuggesting: true }))
  try {
    const cwd = await $.session.cwd()
    const readOr = (path: string) => $.fs.read(path).catch(() => '')
    const pin = (await readOr(`${cwd}/.engram-project`)).trim()
    const head = (await readOr(`${cwd}/.git/HEAD`)).trim().replace(/^ref: refs\/heads\//, '')
    const asks = (await $.session.messages())
      .filter(m => m.role === 'user' && m.text.trim())
      .slice(-8)
      .map(m => `- ${m.text.replace(/\s+/g, ' ').slice(0, 240)}`)
    const d = await read($, draft)
    const prompt = [
      'Suggest 3 to 5 short topic tags (1 to 3 words each, lowercase) for what this person is working on,',
      'for a status card colleagues will see. Prefer concrete product or technical areas over generic words.',
      'Reply with only a JSON array of strings.',
      '',
      `They say they are working on: ${d.workingOn || '(not written yet)'}`,
      `Folder: ${cwd.split(/[\\/]/).pop() ?? ''}${pin ? `, project: ${pin}` : ''}${head && !head.includes('/') ? `, branch: ${head}` : ''}`,
      asks.length ? `Their recent requests in this session:\n${asks.join('\n')}` : '',
    ].join('\n')
    const reply = await $.model.complete({ model: 'haiku', prompt, maxTokens: 200 })
    const topics = reply.isAnswered ? parseTopicReply(reply.text) : []
    if (!topics.length) await update($, note, () => 'No topic suggestions came back; add your own.')
    await update($, draft, cur => ({
      ...cur,
      topics: cleanTopics([...cur.topics, ...topics]),
      chosen: cleanTopics([...cur.chosen, ...topics]),
    }))
  } catch (err) {
    await fail($, 'Topic suggestion failed', err)
  } finally {
    await update($, draft, d => ({ ...d, isSuggesting: false }))
  }
}

async function publish($: $) {
  const d = await read($, draft)
  const handle = await read($, me)
  const workingOn = cleanWorkingOn(d.workingOn)
  if (!workingOn) return update($, note, () => 'Write one line about what you are working on first.')
  if (!handle) return update($, note, () => 'Not signed in to Engram yet; press Refresh.')
  const card: Card = { handle, workingOn, topics: cleanTopics(d.chosen), space, updated: new Date().toISOString() }
  await update($, busy, () => 'Publishing your card…')
  try {
    await engram($, 'kb_write', {
      path: CARD_PATH,
      content: cardMarkdown(card),
      message: `status: @${handle} checked in to ${space}`,
      description: workingOn,
    })
    await engram($, 'kb_publish', { path: CARD_PATH, visibility: 'public' })
    await update($, myCard, () => card)
    await update($, view, () => 'space' as View)
    $.ui.toast(`Checked in to ${space}`)
    await update($, busy, () => '')
    await loadSpace($)
  } catch (err) {
    await fail($, 'Publishing failed', err)
  } finally {
    await update($, busy, () => '')
  }
}

async function startCheckIn($: $) {
  const card = await read($, myCard)
  await update($, draft, d => ({
    workingOn: d.workingOn || card?.workingOn || '',
    topics: cleanTopics([...(card?.topics ?? []), ...d.topics]),
    chosen: d.chosen.length ? d.chosen : (card?.topics ?? []),
    isSuggesting: false,
  }))
  await update($, view, () => 'checkin' as View)
}

// --- Talk ----------------------------------------------------------

async function refreshContacts($: $) {
  const c = readContacts(await engram($, 'kb_contacts'))
  await update($, contacts, () => c)
  if (seenIncoming) for (const h of c.incoming) if (!seenIncoming.has(h)) $.ui.toast(`@${h} wants to connect`)
  seenIncoming = new Set(c.incoming)
}

async function connect($: $, handle: string, isAccept: boolean) {
  try {
    await engram($, isAccept ? 'kb_accept_contact' : 'kb_add_contact', { handle })
    await refreshContacts($)
  } catch (err) {
    await fail($, isAccept ? 'Accept failed' : 'Connect failed', err)
  }
}

async function loadChat($: $, handle: string) {
  const messages = readMessages(await engram($, 'kb_messages', { with_handle: handle }))
  await update($, chat, cur => (cur && cur.with === handle ? { with: handle, messages } : cur))
}

async function openChat($: $, handle: string) {
  await update($, chat, () => ({ with: handle, messages: [] } as Chat))
  await update($, view, () => 'chat' as View)
  try {
    await loadChat($, handle)
    await update($, conversations, list => list.map(c => (c.with === handle ? { ...c, unread: 0 } : c)))
    seenUnread?.set(handle, 0)
  } catch (err) {
    await fail($, 'Could not open the conversation', err)
  }
}

async function send($: $, text: string) {
  const body = text.trim()
  const open = await read($, chat)
  if (!body || !open) return
  try {
    await engram($, 'kb_dm', { to: open.with, message: body })
    await loadChat($, open.with)
  } catch (err) {
    await fail($, 'Not sent', err)
  }
}

// --- The live loop: zero model tokens ----------------------------------

async function poll($: $) {
  if (isPolling) return
  isPolling = true
  try {
    const list = readConversations(await engram($, 'kb_messages'))
    const open = await read($, chat)
    const isChatShown = (await read($, view)) === 'chat'
    for (const c of list) {
      const before = seenUnread?.get(c.with)
      const isOpenHere = isChatShown && open?.with === c.with
      if (c.unread > 0 && isOpenHere) await loadChat($, c.with)
      else if (seenUnread && c.unread > (before ?? 0)) $.ui.toast(`@${c.with}: ${c.last}`, { timeoutMs: 6000 })
    }
    seenUnread = new Map(list.map(c => [c.with, c.unread]))
    const shown = list.map(c => (isChatShown && open?.with === c.with ? { ...c, unread: 0 } : c))
    await update($, conversations, () => shown)
    const unread = shown.reduce((n, c) => n + c.unread, 0)
    $.ui.status(unread ? `✉ ${unread} unread · /people` : undefined)
    if (pollCount++ % CONTACTS_EVERY === 0) await refreshContacts($)
  } catch {
    // A missed poll is retried by the next tick; the pane shows real errors.
  } finally {
    isPolling = false
  }
}

export const register: Register = (on, options) => {
  server = String(options.server || 'claude.ai engram')
  space = String(options.space || 'vibechk')
  isPolling = false
  pollCount = 0
  seenUnread = null
  seenIncoming = null

  // --- Wiring ------------------------------------------------------------

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'people',
      description: 'Open the People pane: check in, see your space, overlaps and messages (`/people checkin` to update your card)',
    })
    void poll($)
    $.clock.every(POLL_MS, () => void poll($))
    return next(e)
  })

  on('command.run', { command: 'people' }, async ($, e) => {
    const opened = await $.ui.open({ id: PANE, title: 'People', focus: true })
    if (e.args.trim() === 'checkin') await startCheckIn($)
    else if ((await read($, view)) === 'chat') await update($, view, () => 'space' as View)
    void loadSpace($, e.args.trim() !== 'checkin').then(async () => {
      if ((await read($, view)) === 'checkin') await startCheckIn($)
    })
    return { text: opened.isPlaced ? 'People pane opened.' : 'People pane is waiting for room; widen the terminal.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const elements = $.ui.resolve(e)
    const { Box, Text, Button } = elements
    const Input = 'Input' in elements ? elements.Input : null
    const width = Math.max(24, e.props.bodyColumns ?? e.viewport?.columns ?? 60)
    const rows = e.viewport?.rows ?? 30
    const [v, handle, card, members, over, graph, convs, open, d, n, b] = await Promise.all([
      read($, view),
      read($, me),
      read($, myCard),
      read($, people),
      read($, overlap),
      read($, contacts),
      read($, conversations),
      read($, chat),
      read($, draft),
      read($, note),
      read($, busy),
    ])
    const now = Date.now()
    const rule = <Text dimColor>{'─'.repeat(Math.min(width, 60))}</Text>

    const header = (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={1}>
          <Text bold>People</Text>
          <Text dimColor>
            {handle ? `@${handle}` : 'not signed in'} · space {space}
          </Text>
        </Box>
        {b ? <Text color="cyan">{b}</Text> : null}
        {n ? <Text color="yellow">{n}</Text> : null}
      </Box>
    )

    if (v === 'checkin') {
      const preview = cleanWorkingOn(d.workingOn)
      return (
        <Box flexDirection="column">
          {header}
          {rule}
          <Text bold>What are you working on?</Text>
          {Input ? (
            <Input
              key="working-on"
              value={d.workingOn}
              placeholder="one line, e.g. the people pane for Monday's demo"
              submitLabel="save"
              autoFocus
              onInput={value => void update($, draft, cur => ({ ...cur, workingOn: value }))}
              onSubmit={value => void update($, draft, cur => ({ ...cur, workingOn: value }))}
            />
          ) : (
            <Text dimColor>Typing needs the terminal or desktop.</Text>
          )}
          <Box marginTop={1} flexDirection="column">
            <Text bold>Topics</Text>
            <Text dimColor>Press a topic to include or drop it.</Text>
            <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
              {d.topics.map(topic => {
                const isChosen = d.chosen.includes(topic)
                return (
                  <Button
                    key={`topic-${topic}`}
                    dimColor={!isChosen}
                    onPress={() =>
                      void update($, draft, cur => ({
                        ...cur,
                        chosen: cur.chosen.includes(topic) ? cur.chosen.filter(t => t !== topic) : [...cur.chosen, topic],
                      }))
                    }
                  >
                    {`${isChosen ? '✓' : '·'} ${topic}`}
                  </Button>
                )
              })}
            </Box>
            {Input ? (
              <Input
                key="add-topic"
                value=""
                placeholder="add a topic"
                submitLabel="add"
                onSubmit={value => {
                  const topic = cleanTopic(value)
                  if (!topic) return
                  void update($, draft, cur => ({
                    ...cur,
                    topics: cleanTopics([...cur.topics, topic]),
                    chosen: cleanTopics([...cur.chosen, topic]),
                  }))
                }}
              />
            ) : null}
            <Button key="suggest" hotkey="s" dimColor={d.isSuggesting} onPress={() => void suggestTopics($)}>
              {d.isSuggesting ? 'Suggesting…' : 'Suggest topics from my work'}
            </Button>
          </Box>
          <Box marginTop={1} flexDirection="column" borderStyle="round" paddingX={1}>
            <Text dimColor>Publishing makes this public to everyone signed in to Engram:</Text>
            <Text>{`@${handle || '?'} is working on: ${preview || '…'}`}</Text>
            <Text>{`topics: ${d.chosen.length ? d.chosen.join(', ') : '(none)'}`}</Text>
            <Text>{`space: ${space}`}</Text>
          </Box>
          <Box flexDirection="row" gap={1}>
            <Button key="publish" hotkey="p" variant="primary" onPress={() => void publish($)}>
              Publish card
            </Button>
            <Button key="back" hotkey="b" onPress={() => void update($, view, () => 'space' as View)}>
              Back
            </Button>
          </Box>
        </Box>
      )
    }

    if (v === 'chat' && open) {
      const shown = open.messages.slice(-Math.max(4, rows - 9))
      return (
        <Box flexDirection="column">
          {header}
          {rule}
          <Box flexDirection="row" gap={1}>
            <Text bold>{`@${open.with}`}</Text>
            <Button key="back" hotkey="b" onPress={() => void update($, view, () => 'space' as View)}>
              Back
            </Button>
          </Box>
          {shown.length === 0 ? <Text dimColor>No messages yet. Say hello.</Text> : null}
          {shown.map((m, i) => (
            <Box key={`m-${i}`} flexDirection="row" gap={1}>
              <Text color={m.from === handle ? 'cyan' : 'magenta'}>{`@${m.from}`}</Text>
              <Text wrap="wrap">{m.body}</Text>
            </Box>
          ))}
          {Input ? (
            <Input
              key="dm"
              value=""
              placeholder={`message @${open.with}`}
              submitLabel="send"
              autoFocus
              onSubmit={value => void send($, value)}
            />
          ) : null}
        </Box>
      )
    }

    // The space.
    const isContact = (h: string) => graph.contacts.includes(h)
    const unreadOf = (h: string) => convs.find(c => c.with === h)?.unread ?? 0
    const between: string[] = []
    for (let i = 0; i < members.length; i++)
      for (let j = i + 1; j < members.length; j++) {
        const both = sharedTopics(members[i]!.card.topics, members[j]!.card.topics)
        if (both.length) between.push(`@${members[i]!.handle} + @${members[j]!.handle}: both on ${both.join(', ')}`)
      }
    const strangers = convs.filter(c => !members.some(p => p.handle === c.with))

    return (
      <Box flexDirection="column">
        {header}
        {rule}
        <Box flexDirection="column">
          <Text bold>You</Text>
          {card ? (
            <Box flexDirection="column">
              <Text wrap="wrap">{`on ${card.workingOn}`}</Text>
              <Text dimColor>{`topics: ${card.topics.join(', ') || '(none)'} · ${ago(null, card.updated, now)}`}</Text>
            </Box>
          ) : (
            <Text dimColor>Not checked in yet.</Text>
          )}
          <Box flexDirection="row" gap={1}>
            <Button key="checkin" hotkey="c" variant={card ? undefined : 'primary'} onPress={() => void startCheckIn($)}>
              {card ? 'Update my card' : 'Check in'}
            </Button>
            <Button key="refresh" hotkey="r" onPress={() => void loadSpace($)}>
              Refresh
            </Button>
          </Box>
        </Box>
        {rule}
        <Text bold>{`In ${space} (${members.length})`}</Text>
        {members.length === 0 ? <Text dimColor>{`Nobody else has checked in to ${space} yet.`}</Text> : null}
        {members.map(p => {
          const o = over[p.handle]
          const hasOverlap = !!o && (o.shared.length > 0 || o.pairs.length > 0)
          const unread = unreadOf(p.handle)
          return (
            <Box key={`p-${p.handle}`} flexDirection="column" marginTop={1}>
              <Box flexDirection="row" gap={1}>
                <Text bold>{`@${p.handle}`}</Text>
                <Text dimColor>{ago(p.minutesAgo, p.card.updated, now)}</Text>
              </Box>
              <Text wrap="wrap">{`on ${p.card.workingOn}`}</Text>
              <Text dimColor>{`topics: ${p.card.topics.join(', ') || '(none)'}`}</Text>
              {hasOverlap ? (
                <Box flexDirection="column" paddingLeft={2}>
                  <Text color="green">You two should talk</Text>
                  {o!.shared.length ? <Text>{`both on: ${o!.shared.join(', ')}`}</Text> : null}
                  {o!.pairs.map((pair, i) => (
                    <Text key={`pair-${p.handle}-${i}`} wrap="truncate-end">{`your "${pair.mine}" ~ their "${pair.theirs}"`}</Text>
                  ))}
                </Box>
              ) : o && !o.isLoaded ? (
                <Text dimColor>checking overlap…</Text>
              ) : null}
              <Box flexDirection="row" gap={1}>
                {isContact(p.handle) ? (
                  <Button key={`msg-${p.handle}`} variant={unread ? 'primary' : undefined} onPress={() => void openChat($, p.handle)}>
                    {unread ? `Message (${unread} new)` : 'Message'}
                  </Button>
                ) : graph.incoming.includes(p.handle) ? (
                  <Button key={`accept-${p.handle}`} variant="primary" onPress={() => void connect($, p.handle, true)}>
                    Accept request
                  </Button>
                ) : graph.outgoing.includes(p.handle) ? (
                  <Text dimColor>request sent</Text>
                ) : (
                  <Button key={`connect-${p.handle}`} onPress={() => void connect($, p.handle, false)}>
                    Connect
                  </Button>
                )}
              </Box>
            </Box>
          )
        })}
        {between.length ? (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>Overlaps in the space</Text>
            {between.map((line, i) => (
              <Text key={`between-${i}`} dimColor>
                {line}
              </Text>
            ))}
          </Box>
        ) : null}
        {strangers.length || graph.incoming.some(h => !members.some(p => p.handle === h)) ? (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>Messages</Text>
            {graph.incoming
              .filter(h => !members.some(p => p.handle === h))
              .map(h => (
                <Button key={`accept-x-${h}`} onPress={() => void connect($, h, true)}>
                  {`Accept @${h}`}
                </Button>
              ))}
            {strangers.map(c => (
              <Button key={`conv-${c.with}`} onPress={() => void openChat($, c.with)}>
                {`@${c.with}${c.unread ? ` (${c.unread} new)` : ''}: ${c.last}`.slice(0, width - 4)}
              </Button>
            ))}
          </Box>
        ) : null}
      </Box>
    )
  })
}
