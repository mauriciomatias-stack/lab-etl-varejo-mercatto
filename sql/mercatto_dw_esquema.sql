--
-- PostgreSQL database dump
--


-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: dw; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA dw;


--
-- Name: qa; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA qa;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: dim_canal; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.dim_canal (
    canal_sk integer NOT NULL,
    canal_origem text NOT NULL,
    canal_conformado text NOT NULL,
    sistema text NOT NULL
);


--
-- Name: dim_canal_canal_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.dim_canal_canal_sk_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dim_canal_canal_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.dim_canal_canal_sk_seq OWNED BY dw.dim_canal.canal_sk;


--
-- Name: dim_cliente; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.dim_cliente (
    cliente_sk bigint NOT NULL,
    cpf_normalizado character(11),
    cpf_valido boolean,
    nome_padronizado text NOT NULL,
    cidade text,
    uf character(2),
    dt_primeiro_cadastro date,
    id_legado integer,
    id_ecommerce bigint,
    origem_unificacao text NOT NULL,
    regra_pareamento text,
    CONSTRAINT ck_origem CHECK ((origem_unificacao = ANY (ARRAY['LEGADO'::text, 'ECOM'::text, 'AMBOS'::text])))
);


--
-- Name: dim_cliente_cliente_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.dim_cliente_cliente_sk_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dim_cliente_cliente_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.dim_cliente_cliente_sk_seq OWNED BY dw.dim_cliente.cliente_sk;


--
-- Name: dim_produto; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.dim_produto (
    produto_sk bigint NOT NULL,
    sku text,
    descricao text NOT NULL,
    categoria_nivel1 text,
    categoria_nivel2 text,
    categoria_origem text,
    preco_referencia numeric(12,2),
    ativo boolean,
    id_legado integer,
    id_ecommerce bigint,
    origem text NOT NULL
);


--
-- Name: dim_produto_produto_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.dim_produto_produto_sk_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dim_produto_produto_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.dim_produto_produto_sk_seq OWNED BY dw.dim_produto.produto_sk;


--
-- Name: dim_status; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.dim_status (
    status_sk integer NOT NULL,
    status_origem text NOT NULL,
    sistema text NOT NULL,
    status_conformado text NOT NULL,
    motivo_cancelamento text
);


--
-- Name: dim_status_status_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.dim_status_status_sk_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dim_status_status_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.dim_status_status_sk_seq OWNED BY dw.dim_status.status_sk;


--
-- Name: dim_tempo; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.dim_tempo (
    data_sk date NOT NULL,
    ano integer NOT NULL,
    trimestre integer NOT NULL,
    mes integer NOT NULL,
    dia integer NOT NULL,
    dia_semana integer NOT NULL,
    nome_mes text NOT NULL,
    eh_fim_semana boolean NOT NULL
);


--
-- Name: fato_atendimento; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.fato_atendimento (
    atendimento_sk bigint NOT NULL,
    protocolo text NOT NULL,
    cliente_sk bigint,
    pedido_sk bigint,
    data_abertura_sk date NOT NULL,
    data_fechamento_sk date,
    canal text,
    motivo_conformado text,
    nota_satisfacao integer,
    horas_resolucao numeric(10,2)
);


--
-- Name: fato_atendimento_atendimento_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.fato_atendimento_atendimento_sk_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fato_atendimento_atendimento_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.fato_atendimento_atendimento_sk_seq OWNED BY dw.fato_atendimento.atendimento_sk;


--
-- Name: fato_avaliacao; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.fato_avaliacao (
    avaliacao_sk bigint NOT NULL,
    review_id text NOT NULL,
    pedido_sk bigint,
    produto_sk bigint,
    data_sk date NOT NULL,
    nota integer,
    compra_verificada boolean,
    votos_uteis integer
);


--
-- Name: fato_avaliacao_avaliacao_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.fato_avaliacao_avaliacao_sk_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fato_avaliacao_avaliacao_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.fato_avaliacao_avaliacao_sk_seq OWNED BY dw.fato_avaliacao.avaliacao_sk;


--
-- Name: fato_item_pedido; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.fato_item_pedido (
    item_sk bigint NOT NULL,
    pedido_sk bigint NOT NULL,
    produto_sk bigint NOT NULL,
    quantidade integer NOT NULL,
    valor_unitario numeric(12,2) NOT NULL,
    valor_desconto numeric(12,2) NOT NULL,
    valor_linha numeric(12,2) NOT NULL
);


--
-- Name: fato_item_pedido_item_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.fato_item_pedido_item_sk_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fato_item_pedido_item_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.fato_item_pedido_item_sk_seq OWNED BY dw.fato_item_pedido.item_sk;


--
-- Name: fato_pedido; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.fato_pedido (
    pedido_sk bigint NOT NULL,
    id_origem text NOT NULL,
    sistema_origem text NOT NULL,
    cliente_sk bigint NOT NULL,
    data_pedido_sk date NOT NULL,
    data_entrega_sk date,
    canal_sk integer NOT NULL,
    status_sk integer NOT NULL,
    qtd_itens integer NOT NULL,
    valor_mercadoria numeric(12,2) NOT NULL,
    valor_frete numeric(12,2) NOT NULL,
    valor_desconto numeric(12,2) NOT NULL,
    valor_total numeric(12,2) NOT NULL,
    dias_entrega integer
);


--
-- Name: fato_pedido_pedido_sk_seq; Type: SEQUENCE; Schema: dw; Owner: -
--

CREATE SEQUENCE dw.fato_pedido_pedido_sk_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fato_pedido_pedido_sk_seq; Type: SEQUENCE OWNED BY; Schema: dw; Owner: -
--

ALTER SEQUENCE dw.fato_pedido_pedido_sk_seq OWNED BY dw.fato_pedido.pedido_sk;


--
-- Name: ponte_cliente; Type: TABLE; Schema: dw; Owner: -
--

CREATE TABLE dw.ponte_cliente (
    sistema text NOT NULL,
    id_origem bigint NOT NULL,
    cliente_sk bigint NOT NULL
);


--
-- Name: execucao_assercao; Type: TABLE; Schema: qa; Owner: -
--

CREATE TABLE qa.execucao_assercao (
    id bigint NOT NULL,
    executado_em timestamp with time zone DEFAULT now() NOT NULL,
    etapa text NOT NULL,
    nome text NOT NULL,
    descricao text,
    valor_obtido numeric,
    valor_esperado text,
    aprovado boolean NOT NULL
);


--
-- Name: execucao_assercao_id_seq; Type: SEQUENCE; Schema: qa; Owner: -
--

CREATE SEQUENCE qa.execucao_assercao_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: execucao_assercao_id_seq; Type: SEQUENCE OWNED BY; Schema: qa; Owner: -
--

ALTER SEQUENCE qa.execucao_assercao_id_seq OWNED BY qa.execucao_assercao.id;


--
-- Name: item_orfao; Type: TABLE; Schema: qa; Owner: -
--

CREATE TABLE qa.item_orfao (
    sistema text NOT NULL,
    id_item bigint,
    id_pedido bigint NOT NULL,
    id_produto bigint,
    quantidade bigint,
    valor_unitario numeric(14,2),
    desconto_origem numeric(14,2),
    valor_bruto numeric(14,2),
    motivo text DEFAULT 'PEDIDO_INEXISTENTE_NA_ORIGEM'::text NOT NULL,
    quarentenado_em timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: dim_canal canal_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.dim_canal ALTER COLUMN canal_sk SET DEFAULT nextval('dw.dim_canal_canal_sk_seq'::regclass);


--
-- Name: dim_cliente cliente_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.dim_cliente ALTER COLUMN cliente_sk SET DEFAULT nextval('dw.dim_cliente_cliente_sk_seq'::regclass);


--
-- Name: dim_produto produto_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.dim_produto ALTER COLUMN produto_sk SET DEFAULT nextval('dw.dim_produto_produto_sk_seq'::regclass);


--
-- Name: dim_status status_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.dim_status ALTER COLUMN status_sk SET DEFAULT nextval('dw.dim_status_status_sk_seq'::regclass);


--
-- Name: fato_atendimento atendimento_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.fato_atendimento ALTER COLUMN atendimento_sk SET DEFAULT nextval('dw.fato_atendimento_atendimento_sk_seq'::regclass);


--
-- Name: fato_avaliacao avaliacao_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.fato_avaliacao ALTER COLUMN avaliacao_sk SET DEFAULT nextval('dw.fato_avaliacao_avaliacao_sk_seq'::regclass);


--
-- Name: fato_item_pedido item_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.fato_item_pedido ALTER COLUMN item_sk SET DEFAULT nextval('dw.fato_item_pedido_item_sk_seq'::regclass);


--
-- Name: fato_pedido pedido_sk; Type: DEFAULT; Schema: dw; Owner: -
--

ALTER TABLE ONLY dw.fato_pedido ALTER COLUMN pedido_sk SET DEFAULT nextval('dw.fato_pedido_pedido_sk_seq'::regclass);


--
-- Name: execucao_assercao id; Type: DEFAULT; Schema: qa; Owner: -
--

ALTER TABLE ONLY qa.execucao_assercao ALTER COLUMN id SET DEFAULT nextval('qa.execucao_assercao_id_seq'::regclass);


--
-- Data for Name: dim_canal; Type: TABLE DATA; Schema: dw; Owner: -
--

