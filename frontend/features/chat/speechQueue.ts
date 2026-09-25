type Job = {start:()=>void};
const queues = new Map<string, Job[]>();
export const liveSpeechIds = new Set<string>();
export function enqueueSpeech(key:string, start:(done:()=>void)=>void) {
  const queue = queues.get(key) || [];
  queues.set(key, queue);
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    const first = queue[0] === job;
    const index = queue.indexOf(job);
    if (index >= 0) queue.splice(index,1);
    if (first && queue.length) queue[0]!.start();
    if (!queue.length) queues.delete(key);
  };
  // Each job keeps its own release closure, including when started by its predecessor.
  const job:Job = {start: () => start(finish)};
  queue.push(job);
  if (queue.length === 1) job.start();
  return finish;
}
