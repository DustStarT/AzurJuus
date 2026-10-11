export type EventListener<T> = (payload: T) => void | Promise<void>;
export type Disposer = () => void;
