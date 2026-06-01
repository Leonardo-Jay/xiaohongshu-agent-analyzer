export function getVisibleHotspotGroups(groups) {
  if (!Array.isArray(groups)) return []

  const visible = groups
    .filter(group => Array.isArray(group?.items) && group.items.length >= 3)
    .slice(0, 4)

  return visible.length >= 3 ? visible : []
}

export function getHotspotLayoutClass(groups) {
  const count = Array.isArray(groups) ? groups.length : 0
  return `hotspot-count-${count}`
}
