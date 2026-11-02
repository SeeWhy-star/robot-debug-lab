import React from "react";
import type { Robot } from "./types";
import { robotShort } from "./format";

const COLORS = ["#d9480f", "#1c5bd8"];

export default function Scene({ robots, activeTargets, selected }: {
  robots: Robot[];
  activeTargets: Record<string, number[]>;
  selected: string;
}) {
  // Exact transform for the Nav2 map: 10 m × 8 m, origin (-5, -4).
  const sx = (x: number) => 315 + x * 50;
  const sy = (y: number) => 225 - y * 50;
  return (
    <svg viewBox="30 4 560 456" role="img" aria-label="Nav2 实际占据栅格、机器人实时轨迹和目标位置">
      <defs>
        <pattern id="minor" width="25" height="25" patternUnits="userSpaceOnUse" x="65" y="25">
          <path d="M 25 0 H 0 V 25" fill="none" stroke="#ebe7dc" strokeWidth=".6" />
        </pattern>
      </defs>
      <rect x="65" y="25" width="500" height="400" fill="#fffdf8" />
      <rect x="65" y="25" width="500" height="400" fill="url(#minor)" />
      {[-4, -3, -2, -1, 0, 1, 2, 3, 4].map((n) => (
        <line key={"v" + n} x1={sx(n)} x2={sx(n)} y1="25" y2="425" stroke="#ddd8cb" strokeWidth=".7" />
      ))}
      {[-3, -2, -1, 0, 1, 2, 3].map((n) => (
        <line key={"h" + n} y1={sy(n)} y2={sy(n)} x1="65" x2="565" stroke="#ddd8cb" strokeWidth=".7" />
      ))}
      <image href="/assets/rdl-warehouse-map-light.png" x="65" y="25" width="500" height="400"
        preserveAspectRatio="none" style={{ imageRendering: "pixelated" }} />
      <rect x="65" y="25" width="500" height="400" fill="none" stroke="#26241f" strokeWidth="1" />
      <g className="axis">
        {[-4, -2, 0, 2, 4].map((n) => (
          <text key={"x" + n} x={sx(n)} y="442" textAnchor="middle">{n}</text>
        ))}
        {[-3, -1, 1, 3].map((n) => (
          <text key={"y" + n} x="56" y={sy(n) + 3} textAnchor="end">{n}</text>
        ))}
        <text x="565" y="456" textAnchor="end">x / m</text>
        <text x="60" y="16" textAnchor="end">y / m</text>
      </g>
      <g className="place">
        <text x={sx(3.2)} y={sy(1.6) - 6} textAnchor="middle">货架 A</text>
        <text x={sx(0.55)} y={sy(-0.15) - 6} textAnchor="middle">障碍箱</text>
        <rect x={sx(-4.3) - 11} y={sy(-3.15) - 11} width="22" height="22" fill="none"
          stroke="#2b7a4b" strokeWidth="1.2" strokeDasharray="3 2" />
        <text x={sx(-4.3) - 11} y={sy(-3.15) - 16} fill="#2b7a4b">充电站</text>
      </g>
      {robots.map((r, i) => {
        const goal = activeTargets[r.id];
        if (!goal) return null;
        const color = COLORS[i % 2];
        return (
          <g key={r.id + "goal"} transform={`translate(${sx(goal[0])},${sy(goal[1])})`}>
            <circle r="13" fill="none" stroke={color} strokeWidth="1.4" strokeDasharray="3 3" />
            <path d="M-5 0H5M0-5V5" stroke={color} strokeWidth="1.4" />
            <text x="16" y={i ? 16 : -10} fill={color} className="goal-label">
              {robotShort(r.id)} 目标 ({goal[0]}, {goal[1]})
            </text>
          </g>
        );
      })}
      {robots.map((r, i) => {
        const color = COLORS[i % 2];
        const p = r.pose;
        return (
          <g key={r.id} opacity={selected && selected !== r.id ? 0.55 : 1}>
            <polyline points={r.trace.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" ")}
              fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" opacity=".7" />
            {p && (
              <g transform={`translate(${sx(p.x)},${sy(p.y)})`}>
                <circle r="9" fill="#fffdf8" stroke={color} strokeWidth="1.6" />
                <path d="M7 0L-4 -4.5L-2 0L-4 4.5Z" fill={color}
                  transform={`rotate(${(-p.theta * 180) / Math.PI})`} />
                <text x="13" y={i ? 18 : -12} fill={color} className="robot-label">{robotShort(r.id)}</text>
              </g>
            )}
          </g>
        );
      })}
    </svg>
  );
}
