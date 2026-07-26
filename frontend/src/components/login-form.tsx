"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { signIn } from "@/lib/auth-client";
import { Button } from "@/components/ui/button";
import { IllumiLogo } from "@/components/illumi-logo";

interface LoginFormProps {
  defaultEmail?: string;
  defaultPassword?: string;
  showBootstrapHint?: boolean;
  helperMessage?: string;
}

export function LoginForm({
  defaultEmail = "",
  defaultPassword = "",
  showBootstrapHint = false,
  helperMessage,
}: LoginFormProps) {
  const router = useRouter();
  const [email, setEmail] = useState(defaultEmail);
  const [password, setPassword] = useState(defaultPassword);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setLoading(true);

    const { error: authError } = await signIn.email({
      email,
      password,
      callbackURL: "/",
    });

    if (authError) {
      setError(authError.message || "Erro ao fazer login");
      setLoading(false);
      return;
    }

    router.push("/");
    router.refresh();
  }

  return (
    <div className="flex min-h-screen">
      {/* Painel esquerdo — identidade da marca (escondido no mobile) */}
      <div className="relative hidden w-[45%] overflow-hidden bg-illumi-navy-deep md:flex md:flex-col md:justify-between">
        {/* Glow do gradiente illumi */}
        <div
          aria-hidden
          className="absolute inset-0"
          style={{
            background:
              "radial-gradient(ellipse at 25% 15%, color-mix(in oklab, var(--illumi-bright) 28%, transparent) 0%, transparent 55%), radial-gradient(ellipse at 85% 85%, color-mix(in oklab, var(--illumi-deep) 32%, transparent) 0%, transparent 55%)",
          }}
        />
        {/* Linha gradiente nas bordas */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-y-0 right-0 w-px"
          style={{ background: "var(--illumi-gradient)", opacity: 0.5 }}
        />

        {/* Conteúdo */}
        <div className="relative z-10 flex flex-1 flex-col justify-center px-10 lg:px-14">
          {/* Marca */}
          <div className="mb-10">
            <IllumiLogo variant="wordmark-white" className="h-9 w-auto" />
            <p className="mt-5 text-[10px] font-medium uppercase tracking-[0.22em] text-white/50">
              Harness para agentes de WhatsApp
            </p>
          </div>

          {/* Descrição */}
          <h1 className="mb-4 text-2xl font-semibold leading-tight text-white/95 lg:text-3xl">
            Painel de operações
          </h1>
          <p className="max-w-sm text-sm leading-relaxed text-white/55">
            Métricas, fila de mensagens, conversas e configurações dos agentes
            em uma interface administrativa.
          </p>
        </div>

        {/* Rodapé do painel */}
        <div className="relative z-10 px-10 pb-8 lg:px-14">
          <div
            className="h-px w-12"
            style={{ background: "var(--illumi-gradient)" }}
          />
          <p className="mt-3 text-[11px] tracking-[0.18em] text-white/35">
            ILLUMI · AI
          </p>
        </div>
      </div>

      {/* Painel direito — formulário */}
      <div className="flex flex-1 items-center justify-center px-6 py-12">
        <div className="w-full max-w-sm">
          {/* Marca no mobile */}
          <div className="mb-8 flex justify-center md:hidden">
            <IllumiLogo variant="wordmark" className="h-8 w-auto text-foreground" />
          </div>

          {/* Cabeçalho do form */}
          <div className="mb-6">
            <h2 className="text-xl font-semibold">Entrar</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Acesse o painel administrativo
            </p>
          </div>

          {helperMessage && (
            <div className="mb-4 rounded-lg bg-muted px-4 py-3 text-sm text-muted-foreground">
              {helperMessage}
            </div>
          )}

          {showBootstrapHint && (
            <div className="mb-4 rounded-lg bg-primary/5 border border-primary/10 px-4 py-3 text-sm text-muted-foreground">
              Primeiro admin criado automaticamente a partir de
              <strong> ADMIN_EMAIL</strong> e <strong>ADMIN_PASSWORD</strong>.
              Entre e troque a senha em <strong>/settings</strong>.
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <label htmlFor="email" className="text-sm font-medium">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                required
                className="flex h-10 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-ring/20"
                placeholder="Digite seu email"
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="password" className="text-sm font-medium">
                Senha
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                minLength={8}
                className="flex h-10 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-ring/20"
                placeholder="Digite sua senha"
              />
            </div>

            {error && (
              <div className="rounded-lg bg-destructive/10 px-4 py-3 text-sm text-destructive">
                {error}
              </div>
            )}

            <Button type="submit" className="w-full h-10" disabled={loading}>
              {loading ? "Entrando..." : "Entrar"}
            </Button>
          </form>
        </div>
      </div>
    </div>
  );
}
