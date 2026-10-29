import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useState,
  type ReactNode,
} from "react";
import { Moon, Sun } from "lucide-react";
import { Button } from "../ui/Button";

type Theme = "dark" | "light";
const key = "codespy.appearance.theme";
const ThemeContext = createContext<{ theme: Theme; toggle: () => void } | null>(
  null,
);

function readTheme(): Theme {
  try {
    return window.localStorage.getItem(key) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(key, theme);
    } catch {
      /* Appearance storage is optional. */
    }
  }, [theme]);
  useEffect(() => {
    function sync(event: StorageEvent) {
      if (event.key === key || event.key === null) {
        setTheme(event.newValue === "light" ? "light" : "dark");
      }
    }
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  return (
    <ThemeContext.Provider
      value={{
        theme,
        toggle: () =>
          setTheme((value) => (value === "dark" ? "light" : "dark")),
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
}

export function ThemeToggle() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("ThemeToggle requires ThemeProvider");
  const next = value.theme === "dark" ? "light" : "dark";
  const Icon = next === "light" ? Sun : Moon;
  return (
    <Button onClick={value.toggle} aria-label={`Switch to ${next} theme`}>
      <Icon size={16} aria-hidden="true" />
      <span>{next === "light" ? "Light theme" : "Dark theme"}</span>
    </Button>
  );
}
