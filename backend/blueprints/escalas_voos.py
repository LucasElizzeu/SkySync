"""Blueprint de voos, jornadas e escalas.

Fecha o ciclo de otimizacao:

    voos       -> a malha que precisa de tripulacao
    jornadas   -> o que cada tripulante ja cumpriu (insumo das restricoes)
    escalas    -> o resultado da alocacao, com a analise de conformidade

    GET  /api/voos                  a malha, com filtros
    GET  /api/jornadas              o acumulado por tripulante
    GET  /api/escalas               as alocacoes gravadas
    POST /api/escalas/otimizar      simula uma proposta ou aprova a proposta revisada

Antes deste modulo, voos e jornadas existiam so em memoria durante a chamada da
API — nada persistia, e nao havia como auditar uma decisao do motor depois.
"""
from __future__ import annotations

import json
from datetime import date

from flask import Blueprint, current_app, jsonify, request, session

from blueprints.auth import login_obrigatorio
from db import transacao
from optimizer.restricoes import COMPOSICAO_PADRAO
from optimizer.solver import OtimizacaoInviavel, otimizar

bp = Blueprint("escalas_voos", __name__, url_prefix="/api")


def _formatar_horas(horas: float | None) -> str:
    total_min = int(round((horas or 0) * 60))
    return f"{total_min // 60:02d}h{total_min % 60:02d}"


def _limite_query(padrao: int, teto: int) -> int | None:
    """Le e valida o parametro `limite`. Devolve None quando invalido."""
    try:
        return max(1, min(int(request.args.get("limite", padrao)), teto))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Voos
# ---------------------------------------------------------------------------
@bp.get("/voos")
@login_obrigatorio
def listar_voos():
    """A malha de voos. Query string: origem, destino, noturno, limite."""
    origem = (request.args.get("origem") or "").strip().upper()
    destino = (request.args.get("destino") or "").strip().upper()
    noturno = request.args.get("noturno")

    limite = _limite_query(300, 1000)
    if limite is None:
        return jsonify({"erro": "limite deve ser um número"}), 400

    condicoes, parametros = [], []
    if origem:
        condicoes.append("origem = %s")
        parametros.append(origem)
    if destino:
        condicoes.append("destino = %s")
        parametros.append(destino)
    if noturno in ("1", "true"):
        condicoes.append("pouso_noturno = TRUE")

    onde = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""

    with transacao() as conexao:
        linhas = conexao.execute(
            f"""
            SELECT id, codigo, origem, destino, partida, chegada,
                   duracao_horas, distancia_km, aeronave, pouso_noturno, status
            FROM voos {onde}
            ORDER BY partida, codigo
            LIMIT %s
            """,
            tuple(parametros + [limite]),
        ).fetchall()

        total = conexao.execute(
            f"SELECT COUNT(*) AS n FROM voos {onde}", tuple(parametros)
        ).fetchone()["n"]

    return jsonify({
        "total": total,
        "retornados": len(linhas),
        "voos": [
            {
                "id": l["id"],
                "codigo": l["codigo"],
                "origem": l["origem"],
                "destino": l["destino"],
                "rota": f"{l['origem']} → {l['destino']}",
                "partida": l["partida"],
                "chegada": l["chegada"],
                "duracao_horas": round(l["duracao_horas"], 2),
                "duracao": _formatar_horas(l["duracao_horas"]),
                "distancia_km": l["distancia_km"],
                "aeronave": l["aeronave"],
                "pouso_noturno": bool(l["pouso_noturno"]),
                "status": l["status"],
            }
            for l in linhas
        ],
    })


# ---------------------------------------------------------------------------
# Jornadas
# ---------------------------------------------------------------------------
@bp.get("/jornadas")
@login_obrigatorio
def listar_jornadas():
    """O acumulado de cada tripulante. Filtros: base, cargo, disponiveis."""
    base = (request.args.get("base") or "").strip().upper()
    cargo = (request.args.get("cargo") or "").strip()
    so_disponiveis = request.args.get("disponiveis") in ("1", "true")

    limite = _limite_query(300, 1000)
    if limite is None:
        return jsonify({"erro": "limite deve ser um número"}), 400

    condicoes, parametros = [], []
    if base:
        condicoes.append("t.base = %s")
        parametros.append(base)
    if cargo:
        condicoes.append("t.cargo = %s")
        parametros.append(cargo)
    if so_disponiveis:
        condicoes.append("j.descanso_ok = TRUE")
        condicoes.append("j.aclimatado = TRUE")
        condicoes.append("j.horas_acumuladas < j.limite_horas")

    onde = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""

    with transacao() as conexao:
        linhas = conexao.execute(
            f"""
            SELECT j.tripulante_id, t.nome, t.sobrenome, t.cargo, t.base,
                   j.periodo, j.horas_voadas, j.horas_acumuladas, j.limite_horas,
                   j.voos_no_periodo, j.descanso_ok, j.aclimatado
            FROM jornadas j
            JOIN tripulantes t ON t.id = j.tripulante_id
            {onde}
            ORDER BY t.cargo, t.base, j.tripulante_id
            LIMIT %s
            """,
            tuple(parametros + [limite]),
        ).fetchall()

    return jsonify({
        "retornados": len(linhas),
        "jornadas": [
            {
                "tripulante_id": l["tripulante_id"],
                "nome_completo": f"{l['nome']} {l['sobrenome']}".strip(),
                "cargo": l["cargo"],
                "base": l["base"],
                "periodo": str(l["periodo"]),
                "horas_voadas": round(l["horas_voadas"], 2),
                "horas_acumuladas": round(l["horas_acumuladas"], 2),
                "limite_horas": l["limite_horas"],
                "voos_no_periodo": l["voos_no_periodo"],
                "ocupacao_percentual": round(
                    (l["horas_acumuladas"] / l["limite_horas"] * 100)
                    if l["limite_horas"] else 0.0,
                    1,
                ),
                "descanso_ok": bool(l["descanso_ok"]),
                "aclimatado": bool(l["aclimatado"]),
            }
            for l in linhas
        ],
    })


# ---------------------------------------------------------------------------
# Escalas gravadas
# ---------------------------------------------------------------------------
@bp.get("/escalas/alocacoes")
@login_obrigatorio
def listar_escalas():
    """As alocacoes gravadas. Query string: data, conforme, limite."""
    dia = (request.args.get("data") or "").strip() or str(date.today())
    conforme = request.args.get("conforme")

    limite = _limite_query(200, 1000)
    if limite is None:
        return jsonify({"erro": "limite deve ser um número"}), 400

    condicoes, parametros = ["e.data = %s"], [dia]
    if conforme in ("0", "false"):
        condicoes.append("e.conforme = FALSE")

    with transacao() as conexao:
        linhas = conexao.execute(
            f"""
            SELECT e.id, e.tripulante_id, t.nome, t.sobrenome, e.cargo,
                   e.voo_id, v.codigo AS voo, e.origem, e.destino,
                   e.duracao_horas, e.horas_antes, e.horas_depois,
                   e.limite_aplicado, e.ocupacao_percentual, e.conforme,
                   e.motivo, e.status, e.execucao_id
            FROM escalas e
            JOIN tripulantes t ON t.id = e.tripulante_id
            JOIN voos v ON v.id = e.voo_id
            WHERE {' AND '.join(condicoes)}
            ORDER BY v.partida, e.cargo
            LIMIT %s
            """,
            tuple(parametros + [limite]),
        ).fetchall()

    return jsonify({
        "data": dia,
        "total": len(linhas),
        "conformes": sum(1 for l in linhas if l["conforme"]),
        "escalas": [
            {
                "id": l["id"],
                "tripulante_id": l["tripulante_id"],
                "tripulante": f"{l['nome']} {l['sobrenome']}".strip(),
                "cargo": l["cargo"],
                "voo_id": l["voo_id"],
                "voo": l["voo"],
                "rota": f"{l['origem']} → {l['destino']}",
                "horas": _formatar_horas(l["duracao_horas"]),
                "horas_antes": round(l["horas_antes"], 2),
                "horas_depois": round(l["horas_depois"], 2),
                "limite_aplicado": l["limite_aplicado"],
                "ocupacao_percentual": round(l["ocupacao_percentual"], 1),
                "conforme": bool(l["conforme"]),
                "motivo": l["motivo"],
                "status": l["status"],
            }
            for l in linhas
        ],
    })


# ---------------------------------------------------------------------------
# Otimizacao sobre a malha real — roda e GRAVA
# ---------------------------------------------------------------------------
@bp.post("/escalas/propostas")
@bp.post("/escalas/otimizar")
@login_obrigatorio
def otimizar_e_gravar():
    """Simula por padrão. A aprovação referencia a execução salva."""
    from models import Tripulante, Voo

    corpo = request.get_json(silent=True) if request.data else {}
    if not isinstance(corpo, dict):
        return jsonify({"erro": "corpo deve ser um objeto JSON"}), 400

    gravar = corpo.get("gravar", False)
    if type(gravar) is not bool:
        return jsonify({"erro": "gravar deve ser booleano"}), 400
    if gravar:
        return _aprovar(corpo.get("proposta_id"))
    try:
        dia = date.fromisoformat(str(corpo.get("data") or date.today())).isoformat()
    except ValueError:
        return jsonify({"erro": "Data inválida."}), 400
    base = str(corpo.get("base") or "GRU").strip().upper()
    so_disponiveis = bool(corpo.get("so_disponiveis", True))

    try:
        limite_voos = max(1, min(int(corpo.get("limite_voos", 30)), 200))
    except (TypeError, ValueError):
        return jsonify({"erro": "limite_voos deve ser um número"}), 400

    with transacao() as conexao:
        linhas_voos = conexao.execute(
            """
            SELECT id, codigo, origem, destino, partida, chegada, duracao_horas, pouso_noturno
            FROM voos v WHERE status = 'Programado'
              AND NOT EXISTS (SELECT 1 FROM escalas e WHERE e.voo_id = v.id AND e.data = %s)
            ORDER BY partida, id LIMIT %s
            """,
            (dia, limite_voos),
        ).fetchall()

        if not linhas_voos:
            return jsonify({
                "erro": "Nenhum voo programado disponível para simular.",
            }), 400

        condicao_disp = (
            " AND j.descanso_ok = TRUE AND j.aclimatado = TRUE"
            " AND j.horas_acumuladas < j.limite_horas"
            if so_disponiveis else ""
        )
        linhas_jornadas = conexao.execute(
            f"""
            SELECT j.tripulante_id, t.nome, t.cargo, t.base, t.status,
                   j.horas_acumuladas, j.limite_horas, j.descanso_ok, j.aclimatado
            FROM jornadas j
            JOIN tripulantes t ON t.id = j.tripulante_id
            WHERE j.periodo = %s AND t.status <> 'Indisponível'{condicao_disp}
            """, (dia,)
        ).fetchall()

    voos = [
        Voo(
            id=l["id"], codigo=l["codigo"], origem=l["origem"], destino=l["destino"],
            duracao_horas=l["duracao_horas"], pouso_noturno=bool(l["pouso_noturno"]),
            prioridade=1,
        )
        for l in linhas_voos
    ]

    tripulantes = [
        Tripulante(
            id=l["tripulante_id"], nome=l["nome"], cargo=l["cargo"], base=l["base"],
            horas_acumuladas=l["horas_acumuladas"], limite_horas=l["limite_horas"],
            descanso_ok=bool(l["descanso_ok"]), aclimatado=bool(l["aclimatado"]),
        )
        for l in linhas_jornadas
    ]

    if not tripulantes:
        return jsonify({"erro": "Nenhuma jornada disponível para a data escolhida."}), 409

    try:
        solucao = otimizar(
            base=base,
            tripulantes=tripulantes,
            voos=voos,
            limite_horas=current_app.config["SOLVER_DEFAULT_HOUR_LIMIT"],
            max_time_seconds=current_app.config["SOLVER_MAX_TIME_SECONDS"],
            composicao=COMPOSICAO_PADRAO,
        )
    except OtimizacaoInviavel as err:
        with transacao() as conexao:
            conexao.execute(
                """INSERT INTO execucoes_otimizacao
                   (base, status_solver, total_tripulantes, total_voos)
                   VALUES (%s, 'INFEASIBLE', %s, %s)""",
                (base, len(tripulantes), len(voos)),
            )
        return jsonify({
            "status": "INFEASIBLE",
            "base": base,
            "mensagem": str(err),
            "diagnostico": err.diagnostico,
        }), 409
    except RuntimeError as err:
        return jsonify({"status": "UNKNOWN", "mensagem": str(err)}), 500

    resposta = solucao.to_dict()
    resposta["data"] = dia
    propostas = resposta["alocacoes"]
    jornadas = {j["tripulante_id"]: j for j in linhas_jornadas}
    voos_por_id = {v["id"]: v for v in linhas_voos}
    for item in propostas:
        jornada = jornadas[item["tripulante_id"]]
        item["horas_antes"] = jornada["horas_acumuladas"]
        item["limite_aplicado"] = min(jornada["limite_horas"], current_app.config["SOLVER_DEFAULT_HOUR_LIMIT"])
    detalhes = {
        "usuario_id": session["usuario_id"], "aprovada": False, "resultado": resposta,
        "jornadas": {a["tripulante_id"]: jornadas[a["tripulante_id"]] for a in propostas},
        "voos": {a["voo_id"]: voos_por_id[a["voo_id"]] for a in propostas},
    }
    with transacao() as conexao:
        execucao = conexao.execute(
            """INSERT INTO execucoes_otimizacao
               (base, status_solver, wall_time_seconds, objetivo_valor, conformidade,
                total_tripulantes, total_voos, total_alocacoes, variaveis_criadas,
                total_bloqueados, detalhes_json)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (base, solucao.status, solucao.wall_time_seconds, solucao.objetivo,
             solucao.conformidade, len(tripulantes), len(voos), len(propostas),
             solucao.variaveis_criadas, solucao.tripulantes_bloqueados, json.dumps(detalhes)),
        ).fetchone()
    return jsonify({**resposta, "proposta_id": execucao["id"], "escalas_gravadas": 0})


def _aprovar(proposta_id):
    if type(proposta_id) is not int or not 0 < proposta_id <= 2147483647:
        return jsonify({"erro": "Simule e revise a proposta antes de aprovar."}), 400
    with transacao() as conexao:
        registro = conexao.execute(
            "SELECT detalhes_json FROM execucoes_otimizacao WHERE id = %s FOR UPDATE",
            (proposta_id,),
        ).fetchone()
        proposta = (registro or {}).get("detalhes_json") or {}
        if proposta.get("usuario_id") != session["usuario_id"]:
            return jsonify({"erro": "Proposta não encontrada."}), 404
        resultado = proposta["resultado"]
        dia = resultado["data"]
        itens = resultado["alocacoes"]
        resposta = {"proposta_id": proposta_id, "data": dia, "total_alocacoes": len(itens)}
        if proposta["aprovada"]:
            return jsonify({**resposta, "escalas_gravadas": 0})
        # Mesma ordem de bloqueio em todas as aprovações, inclusive concorrentes.
        voos = conexao.execute(
            """SELECT id, codigo, origem, destino, partida, chegada, duracao_horas, pouso_noturno
               FROM voos WHERE id = ANY(%s) AND status = 'Programado' ORDER BY id FOR UPDATE""",
            (list(proposta["voos"]),),
        ).fetchall()
        jornadas = conexao.execute(
            """SELECT j.tripulante_id, t.nome, t.cargo, t.base, t.status, j.horas_acumuladas,
                      j.limite_horas, j.descanso_ok, j.aclimatado
               FROM jornadas j JOIN tripulantes t ON t.id = j.tripulante_id
               WHERE j.periodo = %s AND j.tripulante_id = ANY(%s)
               ORDER BY j.tripulante_id FOR UPDATE OF j,t""",
            (dia, list(proposta["jornadas"])),
        ).fetchall()
        conflito = conexao.execute(
            "SELECT 1 FROM escalas WHERE data = %s AND voo_id = ANY(%s) LIMIT 1",
            (dia, list(proposta["voos"])),
        ).fetchone()
        if (conflito or {v["id"]: v for v in voos} != proposta["voos"]
                or {j["tripulante_id"]: j for j in jornadas} != proposta["jornadas"]):
            return jsonify({"erro": "Os dados da escala mudaram. Simule novamente."}), 409

        # Verifique toda a proposta antes da primeira escrita.
        for a in itens:
            duracao = proposta["voos"][a["voo_id"]]["duracao_horas"]
            teto = min(a["limite_aplicado"], current_app.config["SOLVER_DEFAULT_HOUR_LIMIT"])
            if a["horas_antes"] + duracao > teto + 1e-9:
                return jsonify({"erro": "A proposta excede o limite de jornada."}), 409
        for a in itens:
            voo = proposta["voos"][a["voo_id"]]
            duracao = voo["duracao_horas"]
            depois = a["horas_antes"] + duracao
            limite = min(a["limite_aplicado"], current_app.config["SOLVER_DEFAULT_HOUR_LIMIT"])
            conexao.execute(
                """INSERT INTO escalas
                   (tripulante_id, voo_id, data, cargo, origem, destino, duracao_horas,
                    horas_antes, horas_depois, limite_aplicado, ocupacao_percentual,
                    conforme, motivo, status, execucao_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,'confirmado',%s)""",
                (a["tripulante_id"], voo["id"], dia, a["cargo"], voo["origem"], voo["destino"],
                 duracao, a["horas_antes"], depois, limite, round(100 * depois / limite, 1),
                 "Proposta aprovada pelo planejador", proposta_id),
            )
            conexao.execute(
                """UPDATE jornadas SET horas_voadas = horas_voadas + %s,
                   horas_acumuladas = horas_acumuladas + %s, voos_no_periodo = voos_no_periodo + 1,
                   atualizado_em = NOW() WHERE tripulante_id = %s AND periodo = %s""",
                (duracao, duracao, a["tripulante_id"], dia),
            )
        proposta["aprovada"] = True
        conexao.execute("UPDATE execucoes_otimizacao SET detalhes_json = %s WHERE id = %s",
                        (json.dumps(proposta), proposta_id))
    return jsonify({**resposta, "escalas_gravadas": len(itens)})
