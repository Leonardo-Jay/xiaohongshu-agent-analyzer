import assert from 'node:assert/strict'
import test from 'node:test'

import { getHotspotLayoutClass, getVisibleHotspotGroups } from './homeHotspots.js'

const group = (title, count) => ({
  title,
  items: Array.from({ length: count }, (_, index) => ({ title: `${title}-${index}` })),
})

test('filters legacy sparse groups before homepage display', () => {
  const visible = getVisibleHotspotGroups([
    group('too-short', 2),
    group('a', 4),
    group('b', 3),
    group('c', 5),
  ])

  assert.deepEqual(visible.map(item => item.title), ['a', 'b', 'c'])
  assert.equal(getHotspotLayoutClass(visible), 'hotspot-count-3')
})

test('keeps at most four healthy groups for the arc layout', () => {
  const visible = getVisibleHotspotGroups([
    group('a', 4),
    group('b', 4),
    group('c', 4),
    group('d', 4),
    group('e', 4),
  ])

  assert.equal(visible.length, 4)
  assert.equal(getHotspotLayoutClass(visible), 'hotspot-count-4')
})

test('hides hotspot area when fewer than three healthy groups exist', () => {
  const visible = getVisibleHotspotGroups([
    group('a', 4),
    group('b', 2),
    group('c', 4),
  ])

  assert.deepEqual(visible, [])
  assert.equal(getHotspotLayoutClass(visible), 'hotspot-count-0')
})
