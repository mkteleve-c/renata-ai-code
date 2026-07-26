import { cn } from "@/lib/utils";

type Variant = "symbol" | "wordmark" | "wordmark-white";

interface IllumiLogoProps {
  variant?: Variant;
  className?: string;
  title?: string;
}

export function IllumiLogo({
  variant = "symbol",
  className,
  title = "illumi",
}: IllumiLogoProps) {
  if (variant === "symbol") {
    return (
      <svg
        viewBox="0 0 154.7 77.6"
        xmlns="http://www.w3.org/2000/svg"
        role="img"
        aria-label={title}
        className={cn("h-6 w-auto shrink-0", className)}
      >
        <defs>
          <linearGradient
            id="illumi-symbol-gradient"
            x1="217.8"
            y1="222"
            x2="327.2"
            y2="112.6"
            gradientTransform="translate(2.9 -272.2) rotate(45)"
            gradientUnits="userSpaceOnUse"
          >
            <stop offset="0" stopColor="#0017ff" />
            <stop offset=".5" stopColor="#0095f2" />
            <stop offset="1" stopColor="#a7e8fe" />
          </linearGradient>
        </defs>
        <path
          fill="url(#illumi-symbol-gradient)"
          d="M88.4,66.3c16.2,16.2,43.1,15.1,57.9-3.3,11.2-14,11.2-34.3,0-48.3-14.7-18.4-41.7-19.5-57.9-3.3-3.4,3.4-6,7.2-7.8,11.3-3.1,6.8-9.7,11.2-17.2,11.2h-2.1c-6.3,0-12.2-3.1-15.7-8.4-1.7-2.6-4-5-6.8-6.9-8.5-5.9-20-5.8-28.3.2-14,10-14,30.6,0,40.6,8.4,6,19.9,6.1,28.3.2,2.8-1.9,5.1-4.3,6.8-6.9,3.4-5.3,9.4-8.4,15.7-8.4h2.3c7.4,0,14,4.4,17.1,11.1,1.8,4,4.4,7.7,7.7,10.9Z"
        />
      </svg>
    );
  }

  const wordFill = variant === "wordmark-white" ? "#ffffff" : "currentColor";

  return (
    <svg
      viewBox="0 0 566.9 128"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label={title}
      className={cn("h-6 w-auto shrink-0", className)}
    >
      <defs>
        <linearGradient
          id="illumi-wordmark-gradient"
          x1="217.8"
          y1="263.5"
          x2="327.2"
          y2="154.1"
          gradientTransform="translate(32.3 -260) rotate(45)"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#0017ff" />
          <stop offset=".5" stopColor="#0095f2" />
          <stop offset="1" stopColor="#a7e8fe" />
        </linearGradient>
      </defs>
      <path
        fill="url(#illumi-wordmark-gradient)"
        d="M88.4,107.8c16.2,16.2,43.1,15.1,57.9-3.3,11.2-14,11.2-34.3,0-48.3-14.7-18.4-41.7-19.5-57.9-3.3-3.4,3.4-6,7.2-7.8,11.3-3.1,6.8-9.7,11.2-17.2,11.2h-2.1c-6.3,0-12.2-3.1-15.7-8.4-1.7-2.6-4-5-6.8-6.9-8.5-5.9-20-5.8-28.3.2-14,10-14,30.6,0,40.6,8.4,6,19.9,6.1,28.3.2,2.8-1.9,5.1-4.3,6.8-6.9,3.4-5.3,9.4-8.4,15.7-8.4h2.3c7.4,0,14,4.4,17.1,11.1,1.8,4,4.4,7.7,7.7,10.9Z"
      />
      <g fill={wordFill}>
        <rect x="552.6" width="14.3" height="17.6" />
        <rect x="552.6" y="34.9" width="14.3" height="91" />
        <path d="M244.1,112.9c-2.5,0-4.6-2.1-4.6-4.6V0h-14.3v107.5c0,10.2,8.2,18.4,18.4,18.4h12.9v-13h-12.5Z" />
        <path d="M393.5,34.9v91h-12.5c0-4.5,2.7-10.7,6.5-16.4h-7.4c-.6,1.8-1.2,3.6-2.2,5.1-5.5,8.7-14,13.3-26.5,13.3-18.7,0-30.6-11.3-30.6-30.1v-63h14.3v62.7c0,11.8,7.6,18.7,20.4,18.7s23.8-10.9,23.8-25.4v-56h14.3Z" />
        <path d="M533.8,62v63.9h-14.3v-63.2c0-11.3-7.6-18-16.7-18s-22.4,10.2-22.4,24.5v56.7h-14.3v-63.2c0-11.3-6.3-18-15.7-18s-23.4,10.2-23.4,24.5v56.7h-14.3V34.9h12.5c0,4.5-2.7,10.7-6.5,16.4h7.4c.5-1.3.9-2.7,1.6-3.8,5-8.9,15.2-14.6,26.4-14.6s19.9,5.6,23.8,16.2h.3c5.8-10.4,16.7-16.2,27.8-16.2,16.2,0,27.6,10.4,27.6,29.2Z" />
        <rect x="191" width="14.3" height="17.6" />
        <rect x="191" y="34.9" width="14.3" height="91" />
        <path d="M291.8,112.9c-2.5,0-4.6-2.1-4.6-4.6V0h-14.3v107.5c0,10.2,8.2,18.4,18.4,18.4h12.9v-13h-12.5Z" />
      </g>
    </svg>
  );
}
