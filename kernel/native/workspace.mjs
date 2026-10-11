import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';

export const name = 'azurjuus-workspace';
export const inject = ['tools'];

/** Native Cordis tool producer. The Python gateway retains authorization and journals. */
export function apply(ctx, config) {
  const endpoint = new URL(config.endpoint);
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(endpoint.hostname)) {
    throw new Error('Workspace gateway must be local');
  }
  const catalog = config.catalog ?? {};
  const actions = Object.keys(catalog);
  if (!actions.length) return;
  const projections = new WeakMap();
  const dispose = ctx.tools.register({
    name: 'workspace',
    description: 'Execute authorized local work. Use the returned callId as verification evidence.\n' +
      actions.map(action => `${action}: ${catalog[action]}`).join('\n'),
    parameters: {
      type: 'object', properties: {
        action: { type: 'string', enum: actions }, args: { type: 'object' },
      }, required: ['action', 'args'], additionalProperties: false,
    },
    output: {
      schema: {},
      render(_args, value) {
        const { result, ...envelope } = value;
        const material = result && typeof result === 'object' && 'image' in result
          ? { ...envelope, result: { ...result, image: '[durable image attachment]' } } : value;
        return [{ type: 'text', text: JSON.stringify(material) }];
      },
    },
    async execute(args, exec) {
      if (!actions.includes(args?.action) || !args.args || typeof args.args !== 'object' || Array.isArray(args.args)) {
        throw new Error('Invalid or unauthorized workspace action');
      }
      const callId = createHash('sha256').update(config.token + ':' + exec.callId).digest('hex');
      const imageCall = args.action === 'read_image' ||
        ['browser', 'desktop'].includes(args.action) && args.args.op === 'screenshot';
      const attachments = ctx.get('attachments');
      if (imageCall) {
        const route = exec.agent?.session.requestHeader()?.config ?? exec.agent?.options;
        const llm = ctx.get('llm');
        const model = route && llm && await llm.resolveModelInfo(route.provider, route.model, exec.signal);
        if (!attachments || !model?.inputModalities?.includes('image')) {
          throw new Error('The current model cannot admit image tool results');
        }
      }
      const response = await fetch(endpoint, {
        method: 'POST', signal: exec.signal,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${config.token}` },
        body: JSON.stringify({ callId, name: args.action, args: args.args }),
      });
      const value = await response.json();
      if (!response.ok || value.status !== 'completed') {
        throw new Error(JSON.stringify({ callId, httpStatus: response.status, ...value }));
      }
      const result = value.result;
      if (result?.image) {
        if (!imageCall) {
          throw new Error('The current model cannot admit image tool results');
        }
        const [attachment] = await attachments.saveImages([
          { data: Buffer.from(result.image, 'base64'), mediaType: result.mimeType },
        ]);
        const { image: _image, ...metadata } = result;
        const projected = [
          { type: 'text', text: JSON.stringify({ ...value, result: metadata }) },
          { type: 'image', attachment },
        ];
        projections.set(exec, { value, content: projected });
      }
      return value;
    },
    projectContent(exec, result) {
      const prepared = projections.get(exec);
      projections.delete(exec);
      if (!result.isError && prepared && isDeepStrictEqual(result.value, prepared.value)) return prepared.content;
    },
  });
  ctx.effect(() => dispose);
}
