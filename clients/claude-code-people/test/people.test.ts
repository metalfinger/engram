import { describe, expect, test } from 'claude-code/testing'

import { cardMarkdown, parseCard, parseTopicReply, sharedTopics } from '../hooks/card'

describe('the status card', () => {
  test('survives a round trip, colons and quotes included', () => {
    const card = {
      handle: 'hiren',
      workingOn: 'people pane: the "Monday" demo',
      topics: ['claude code mods', 'engram'],
      space: 'vibechk',
      updated: '2026-10-03T10:00:00.000Z',
    }
    expect(parseCard('hiren', cardMarkdown(card))).toEqual(card)
  })

  test('a concept that is not a status card is no card', () => {
    expect(parseCard('x', '---\ntype: decision\ntitle: nope\n---\nbody')).toBe(null)
  })

  test('topic suggestions keep only a cleaned JSON array of strings', () => {
    expect(parseTopicReply('Sure! ["Claude Code", "#engram", "engram", 3]')).toEqual(['claude code', 'engram'])
    expect(parseTopicReply('no array here')).toEqual([])
  })

  test('shared topics are the intersection', () => {
    expect(sharedTopics(['a', 'b', 'c'], ['c', 'a'])).toEqual(['a', 'c'])
  })
})

// A fake engram behind $.mcp.call: two people in one space, already contacts.
function fakeEngram() {
  const theirCard = cardMarkdown({
    handle: 'tester',
    workingOn: 'windows install of the people pane',
    topics: ['mods', 'windows'],
    space: 'vibechk',
    updated: '2026-10-03T09:00:00.000Z',
  })
  const myCard = cardMarkdown({
    handle: 'hiren',
    workingOn: 'people pane',
    topics: ['mods', 'engram'],
    space: 'vibechk',
    updated: '2026-10-03T09:30:00.000Z',
  })
  const calls: { tool: string; args: Record<string, unknown> }[] = []
  const thread = [{ from: 'tester', body: 'ping from windows', at: '2026-10-03T09:40:00Z' }]
  const answer = (tool: string, args: Record<string, unknown>): unknown => {
    switch (tool) {
      case 'kb_team':
        return { team: [{ handle: 'tester', minutes_ago: 2 }], me: { handle: 'hiren' } }
      case 'kb_explore':
        return { people: [{ handle: 'tester', display_name: 'Tester' }] }
      case 'kb_contacts':
        return { contacts: ['tester'], incoming: [], outgoing: [] }
      case 'kb_read':
        return { path: args.path, content: myCard }
      case 'kb_read_public':
        return { handle: args.handle, path: args.path, content: theirCard }
      case 'kb_common_ground':
        return { handle: args.handle, pairs: [{ mine: { title: 'People pane spec' }, theirs: { title: 'Windows mod notes' }, score: 0.8 }] }
      case 'kb_messages':
        return args.with_handle
          ? { with: args.with_handle, messages: thread }
          : { conversations: [{ with: 'tester', unread: 1, last: 'ping from windows' }] }
      case 'kb_dm':
        thread.push({ from: 'hiren', body: String(args.message), at: '2026-10-03T09:41:00Z' })
        return { sent: true, to: args.to, at: '2026-10-03T09:41:00Z' }
      default:
        return { ok: true }
    }
  }
  return { calls, answer }
}

const PANE = {
  component: 'Pane',
  requestId: 'people',
  props: {
    title: 'People',
    isFocused: true,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
  viewport: { columns: 100, rows: 40 },
} as const

describe('the pane', () => {
  test('shows the space, the overlap, and talks', async ($, on) => {
    const engram = fakeEngram()
    on('mcp.call', (_$, e) => {
      engram.calls.push({ tool: e.tool, args: e.args })
      return { value: { content: [{ type: 'text', text: JSON.stringify(engram.answer(e.tool, e.args)) }], isError: false } }
    })
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'engram-people', surface, ...(PANE as never) })
      await ui.press({ key: 'refresh' })
      expect((await ui.find({ text: /@tester/ }))?.text).toContain('@tester')
      expect(await ui.find({ text: 'You two should talk' })).toBeDefined()
      expect(await ui.find({ text: 'both on: mods' })).toBeDefined()
      expect(await ui.find({ text: /Windows mod notes/ })).toBeDefined()

      await ui.press({ key: 'msg-tester' })
      expect(await ui.find({ text: 'ping from windows' })).toBeDefined()
      await ui.input({ key: 'dm', text: 'hello back' })
      expect(engram.calls.some(c => c.tool === 'kb_dm' && c.args.to === 'tester' && c.args.message === 'hello back')).toBe(true)
      expect(await ui.find({ text: 'hello back' })).toBeDefined()
      await ui.press({ key: 'back' })
      await ui.unmount()
    }
  })

  test('publishes exactly what the preview shows', async ($, on) => {
    const engram = fakeEngram()
    on('mcp.call', (_$, e) => {
      engram.calls.push({ tool: e.tool, args: e.args })
      return { value: { content: [{ type: 'text', text: JSON.stringify(engram.answer(e.tool, e.args)) }], isError: false } }
    })
    const ui = await $.ui.mount({ plugin: 'engram-people', surface: 'terminal', ...(PANE as never) })
    await ui.press({ key: 'refresh' })
    await ui.press({ key: 'checkin' })
    await ui.input({ key: 'working-on', text: 'demo prep: dry run with the test account' })
    await ui.input({ key: 'add-topic', text: 'Demo' })
    expect(await ui.find({ text: /topics: mods, engram, demo/ })).toBeDefined()
    await ui.press({ key: 'publish' })

    const write = engram.calls.find(c => c.tool === 'kb_write')
    expect(write?.args.path).toBe('self/now.md')
    const card = parseCard('hiren', String(write?.args.content))
    expect(card?.workingOn).toBe('demo prep: dry run with the test account')
    expect(card?.topics).toEqual(['mods', 'engram', 'demo'])
    expect(card?.space).toBe('vibechk')
    expect(engram.calls.some(c => c.tool === 'kb_publish' && c.args.visibility === 'public')).toBe(true)
    await ui.unmount()
  })

  test('an engram error is shown, not swallowed', async ($, on) => {
    on('mcp.call', () => ({ value: { content: [{ type: 'text', text: 'Error executing tool kb_team: boom' }], isError: true } }))
    const ui = await $.ui.mount({ plugin: 'engram-people', surface: 'terminal', ...(PANE as never) })
    await ui.press({ key: 'refresh' })
    expect(await ui.find({ text: /boom/ })).toBeDefined()
    await ui.unmount()
  })
})
