// Agent 显示统一辅助：昵称（名字）+ 头像
import { iconsApi } from '../api'

// 显示名：有昵称 → 「昵称（名字）」；否则只显名字
export function displayName(a) {
  if (!a) return ''
  const name = a.name || a.slug || ''
  const nick = (a.nickname || '').trim()
  return nick ? `${nick}（${name}）` : name
}

// 头像：有自定义头像返回图片 URL，否则返回 null（调用方回退 emoji）
export function avatarUrl(a) {
  if (a && a.avatar) return iconsApi.url(a.avatar)
  return null
}
