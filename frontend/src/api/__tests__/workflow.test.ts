import { describe, it, expect, vi, beforeEach } from 'vitest'

// stub 掉 HTTP 请求层，避免真实网络与重依赖（stores/router/element-plus）
vi.mock('@/api/request', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    patch: vi.fn()
  }
}))

import request from '@/api/request'
import { workflowApi } from '@/api/workflow'

describe('workflowApi.execute', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('后端 data 载荷不含 success 字段时，应补上 success=true 并透传 task_id', async () => {
    // 后端 execute_workflow 的真实返回：data 为 {task_id, status, message}，无 success
    ;(request.post as any).mockResolvedValue({
      success: true,
      data: { task_id: 'T1', status: 'pending', message: '任务已提交，正在后台执行' },
      message: '任务已提交'
    })

    const result = await workflowApi.execute('general_daily_market_review', {})

    expect(result.success).toBe(true)
    expect(result.task_id).toBe('T1')
  })

  it('外层失败时应返回 success=false 并携带错误信息', async () => {
    ;(request.post as any).mockResolvedValue({
      success: false,
      data: null,
      message: '执行失败'
    })

    const result = await workflowApi.execute('wf', {})

    expect(result.success).toBe(false)
    expect(result.error).toBe('执行失败')
  })
})
