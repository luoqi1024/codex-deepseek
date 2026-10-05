/** Installed next to the user's Harness profile. Uses only public plugin services. */
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { setSandboxMode } from '@deepseek-ai/dsh-sandbox-policy';
import { setApprovalPolicy } from '@deepseek-ai/dsh-user-approval';
export const name = 'codex-deepseek-connector';
export const inject = ['workspaceRegistry', 'sessionController', 'agents', 'sessionProjections', 'agentDefaultModel', 'commands', 'tools'];
export async function apply(ctx, config) {
  if (!path.isAbsolute(config.bridgeRoot ?? '')) throw new Error('bridgeRoot must be an absolute path');
  const { install } = await import(pathToFileURL(path.join(config.bridgeRoot, 'native_connector.mjs')).href);
  // Set the two existing enforcement services directly; preserve the user's preset table/default.
  await install(ctx, config, {
    setSandboxMode, setApprovalPolicy,
    computerUseStatus() {
      const provider = ctx.get('computerUse')?.providerName ?? null;
      const tools = ctx.tools.schemas().filter(tool => tool.name.startsWith('cua_driver_native__'));
      return { provider, catalog_size: tools.length,
        tools_ready: provider === 'cua-driver-native' && tools.some(tool => tool.name === 'cua_driver_native__get_window_state'),
        desktop_actions: 0, model_requests: 0 };
    },
  });
}
