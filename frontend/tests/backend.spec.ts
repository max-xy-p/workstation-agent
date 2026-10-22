import { expect, test } from '@playwright/test';

// 联调时显式注入专用离线夹具的令牌，默认测试不访问个人 Agent 服务。
test('真实后端：会话、任务、审批、历史修改与删除', async ({ page }) => {
  test.skip(!process.env.WORKSTATION_AGENT_E2E_TOKEN, '需要专用后端测试服务');
  await page.goto('/');
  await page.getByRole('button', { name: '连接本机 Agent', exact: true }).click();
  await page.getByLabel('访问令牌', { exact: true }).fill(process.env.WORKSTATION_AGENT_E2E_TOKEN ?? '');
  await page.getByRole('button', { name: '连接服务', exact: true }).last().click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '新建会话', exact: true }).click();
  await page.getByRole('textbox', { name: '任务内容' }).fill('测试审批与消息保存');
  await expect(page.getByRole('button', { name: '发送任务', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '发送任务', exact: true }).click();
  await expect(page.getByRole('heading', { name: '需要你的决定' })).toBeVisible({ timeout: 15000 });
  await page.getByRole('radio', { name: '拒绝', exact: true }).check();
  await page.getByRole('button', { name: '提交决定', exact: true }).click();
  await expect(page.getByText('后端联调完成', { exact: true })).toBeVisible({ timeout: 15000 });
  await page.getByRole('button', { name: '补充背景', exact: true }).click();
  await page.getByLabel('消息内容').fill('这是通过真实 HTTP 保存的背景。');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('这是通过真实 HTTP 保存的背景。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '编辑消息', exact: true }).first().click();
  await page.getByLabel('消息内容').fill('编辑后只保留这一条。');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('编辑后只保留这一条。', { exact: true })).toBeVisible();
  await expect(page.getByText('后端联调完成', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '删除当前会话', exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByRole('heading', { name: '从一个具体任务开始' })).toBeVisible();
});
