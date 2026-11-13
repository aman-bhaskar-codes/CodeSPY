import { useRef, type KeyboardEvent, type PointerEvent } from "react";
import styles from "./Splitter.module.css";

interface Props {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  onReset: () => void;
}

/** A vertical separator that resizes the panel on its left: drag, arrow keys, Home/End, double-click to reset. */
export function Splitter({
  label,
  value,
  min,
  max,
  step,
  onChange,
  onReset,
}: Props) {
  const origin = useRef<{ x: number; value: number } | null>(null);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const big = event.shiftKey ? step * 4 : step;
    if (event.key === "ArrowLeft") onChange(value - big);
    else if (event.key === "ArrowRight") onChange(value + big);
    else if (event.key === "Home") onChange(min);
    else if (event.key === "End") onChange(max);
    else if (event.key === "Enter") onReset();
    else return;
    event.preventDefault();
  }
  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    origin.current = { x: event.clientX, value };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }
  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!origin.current) return;
    onChange(origin.current.value + event.clientX - origin.current.x);
  }
  function end(event: PointerEvent<HTMLDivElement>) {
    origin.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      className={styles.splitter}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onDoubleClick={onReset}
    />
  );
}
