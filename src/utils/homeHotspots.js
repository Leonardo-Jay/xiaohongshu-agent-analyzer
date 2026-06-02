export function getVisibleHotspotGroups(groups) {
  if (!Array.isArray(groups)) return []

  const visible = groups
    .filter(group => Array.isArray(group?.items) && group.items.length >= 3)
    .slice(0, 4)

  return visible.length >= 3 ? visible : []
}

export function getHotspotLayoutClass(groups) {
  const count = Array.isArray(groups) ? groups.length : 0
  const itemCounts = Array.isArray(groups)
    ? groups
      .map(group => Array.isArray(group?.items) ? group.items.length : 0)
      .filter(count => count > 0)
    : []
  const itemsPerGroup = count > 0 && itemCounts.length
    ? Math.max(4, Math.min(5, Math.min(...itemCounts)))
    : 0

  return `hotspot-count-${count} hotspot-items-${itemsPerGroup}`
}
