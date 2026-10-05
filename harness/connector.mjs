/** Installed next to the user's Harness profile. Uses only public plugin services. */
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { setSandboxMode } from '@deepseek-ai/dsh-sandbox-policy';
import { setApprovalPolicy } from '@deepseek-ai/dsh-user-approval';
export const name = 'codex-deepseek-connector';
export const inject = ['workspaceRegistry', 'sessionController', 'agents', 'sessionProjections', 'agentDefaultModel', 'commands'];
export async function apply(ctx, config) {
  if (!path.isAbsolute(config.bridgeRoot ?? '')) throw new Error('bridgeRoot must be an absolute path');
  const { install } = await import(pathToFileURL(path.join(config.bridgeRoot, 'native_connector.mjs')).href);
  // Set the two existing enforcement services directly; preserve the user's preset table/default.
  await install(ctx, config, { setSandboxMode, setApprovalPolicy });
}
