import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

const storageKey = 'azur-chat-layout';
export function useChatLayout() {
  const layout = ref<HTMLElement>();
  const panel = ref<HTMLElement>();
  let saved: { list?: number; input?: number } = {};
  try { saved = JSON.parse(localStorage.getItem(storageKey) || '{}') || {}; } catch { /* Defaults remain usable. */ }
  const listWidth = ref(Number.isFinite(saved.list) ? saved.list! : Math.min(420, Math.max(280, innerWidth * .32)));
  const inputHeight = ref(Number.isFinite(saved.input) ? saved.input! : 64);
  const availableWidth = ref(innerWidth - 110);
  const panelHeight = ref(innerHeight - 60);
  const maxList = computed(() => Math.max(220, availableWidth.value - 340));
  const maxInput = computed(() => Math.max(48, Math.min(420, panelHeight.value * .55, panelHeight.value - 220)));
  const clamp = (n: number, min: number, max: number) => Math.min(max, Math.max(min, n));
  function constrain() {
    availableWidth.value = layout.value?.clientWidth || innerWidth - 110;
    panelHeight.value = panel.value?.clientHeight || innerHeight - 60;
    listWidth.value = clamp(listWidth.value, 220, maxList.value);
    inputHeight.value = clamp(inputHeight.value, 48, maxInput.value);
  }
  function save() {
    try { localStorage.setItem(storageKey, JSON.stringify({ list: listWidth.value, input: inputHeight.value })); } catch { /* Session resizing still works. */ }
  }
  let drag: { kind: 'list' | 'input'; origin: number; value: number; target: HTMLElement; pointer: number } | undefined;
  function move(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.pointer) return;
    if (drag.kind === 'list') listWidth.value = clamp(drag.value + event.clientX - drag.origin, 220, maxList.value);
    else inputHeight.value = clamp(drag.value + drag.origin - event.clientY, 48, maxInput.value);
  }
  function finish() {
    const previous = drag;
    drag = undefined;
    previous?.target.removeEventListener('lostpointercapture', finish);
    if (previous?.target.hasPointerCapture(previous.pointer)) previous.target.releasePointerCapture(previous.pointer);
    document.documentElement.classList.remove('resizing-columns', 'resizing-rows');
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', finish);
    window.removeEventListener('pointercancel', finish);
    window.removeEventListener('blur', finish);
    if (previous) save();
  }
  function resize(kind: 'list' | 'input', event: PointerEvent) {
    if (event.button !== 0) return;
    event.preventDefault();
    constrain();
    const target = event.currentTarget as HTMLElement;
    drag = { kind, origin: kind === 'list' ? event.clientX : event.clientY,
      value: kind === 'list' ? listWidth.value : inputHeight.value, target, pointer: event.pointerId };
    target.setPointerCapture(event.pointerId);
    target.addEventListener('lostpointercapture', finish);
    document.documentElement.classList.add(kind === 'list' ? 'resizing-columns' : 'resizing-rows');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
    window.addEventListener('blur', finish);
  }
  function adjust(kind: 'list' | 'input', event: KeyboardEvent) {
    const keys = kind === 'list' ? ['ArrowLeft', 'ArrowRight'] : ['ArrowDown', 'ArrowUp'];
    if (!keys.includes(event.key) && event.key !== 'Home') return;
    event.preventDefault();
    const value = kind === 'list' ? listWidth : inputHeight;
    value.value = event.key === 'Home' ? (kind === 'list' ? 320 : 64) : value.value + (event.key === keys[0] ? -16 : 16);
    constrain(); save();
  }
  function reset(kind: 'list' | 'input') {
    (kind === 'list' ? listWidth : inputHeight).value = kind === 'list' ? 320 : 64;
    constrain(); save();
  }
  let observer: ResizeObserver | undefined;
  watch([layout, panel], ([nextLayout, nextPanel], [oldLayout, oldPanel]) => {
    if (oldLayout) observer?.unobserve(oldLayout);
    if (oldPanel) observer?.unobserve(oldPanel);
    if (nextLayout) observer?.observe(nextLayout);
    if (nextPanel) observer?.observe(nextPanel);
    constrain();
  }, { flush: 'post' });
  onMounted(() => {
    observer = new ResizeObserver(constrain);
    if (layout.value) observer.observe(layout.value);
    if (panel.value) observer.observe(panel.value);
    // The conversation panel can mount after the initial workspace request.
    window.addEventListener('resize', constrain);
    constrain();
  });
  onBeforeUnmount(() => { finish(); observer?.disconnect(); window.removeEventListener('resize', constrain); });
  return { layout, panel, listWidth, inputHeight, maxList, maxInput, resize, adjust, reset, constrain };
}
