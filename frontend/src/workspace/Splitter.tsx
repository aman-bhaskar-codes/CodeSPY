import { useRef, type KeyboardEvent, type PointerEvent } from "react";
import styles from "./Splitter.module.css";

interface Props {
  /** "vertical" resizes the panel on its left; "horizontal" resizes the panel below it (dragging up grows it). */
  orientation?: "vertical" | "horizontal";
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
  orientation = "vertical",
  label,
  value,
  min,
  max,
  step,
  onChange,
  onReset,
}: Props) {
  const horizontal = orientation === "horizontal";
  const origin = useRef<{ at: number; value: number } | null>(null);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const big = event.shiftKey ? step * 4 : step;
    if (event.key === (horizontal ? "ArrowDown" : "ArrowLeft"))
      onChange(value - big);
    else if (event.key === (horizontal ? "ArrowUp" : "ArrowRight"))
      onChange(value + big);
    else if (event.key === "Home") onChange(min);
    else if (event.key === "End") onChange(max);
    else if (event.key === "Enter") onReset();
    else return;
    event.preventDefault();
  }
  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    origin.current = { at: horizontal ? event.clientY : event.clientX, value };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }
  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!origin.current) return;
    const now = horizontal ? event.clientY : event.clientX;
    onChange(
      origin.current.value +
        (horizontal ? origin.current.at - now : now - origin.current.at),
    );
  }
  function end(event: PointerEvent<HTMLDivElement>) {
    origin.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }
  return (
    <div
      role="separator"
      aria-orientation={horizontal ? "horizontal" : "vertical"}
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      className={horizontal ? styles.horizontal : styles.splitter}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onDoubleClick={onReset}
    />
  );
}
