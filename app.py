import json
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound

st.set_page_config(page_title='Confraternização dos Amigos', page_icon='🎉', layout='wide')

HEADERS = ['ID', 'Nome', 'Valor a Pagar (R$)', 'Status de Pagamento', 'Observações']
STATUS = ['Pendente', 'Pago']


def cents(value):
    try:
        number = Decimal(str(value).strip().replace(',', '.'))
        if not number.is_finite() or number < 0:
            raise ValueError('Valor inválido')
        return int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError(f'Valor monetário inválido: {value}')


def brl(value_cents):
    value = f'{value_cents / 100:,.2f}'
    return 'R$ ' + value.replace(',', 'X').replace('.', ',').replace('X', '.')


@st.cache_resource(show_spinner=False)
def get_worksheet():
    info = dict(st.secrets['gcp_service_account'])
    scopes = ['https://www.googleapis.com/auth/spreadsheets',
              'https://www.googleapis.com/auth/drive']
    credentials = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(st.secrets['sheets']['spreadsheet_id'])
    title = st.secrets['sheets'].get('worksheet', 'Participantes')
    try:
        ws = spreadsheet.worksheet(title)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=5)
    if ws.row_values(1) != HEADERS:
        if ws.get_all_values():
            raise ValueError('Cabeçalho da aba não corresponde ao esperado. Verifique a planilha.')
        ws.update(range_name='A1:E1', values=[HEADERS])
    return ws


def load_data(ws):
    records = ws.get_all_records(expected_headers=HEADERS, numericise_ignore=['all'])
    rows = []
    for item in records:
        if not str(item['ID']).strip():
            continue
        rows.append({
            'ID': str(item['ID']).strip(),
            'Nome': str(item['Nome']).strip(),
            'Valor a Pagar (R$)': cents(item['Valor a Pagar (R$)']) / 100,
            'Status de Pagamento': str(item['Status de Pagamento']).strip(),
            'Observações': str(item['Observações']),
        })
    return pd.DataFrame(rows, columns=HEADERS)


def validate(df):
    names = df['Nome'].astype(str).str.strip()
    if names.eq('').any():
        raise ValueError('O nome não pode ficar vazio.')
    if names.str.casefold().duplicated().any():
        raise ValueError('Existem nomes duplicados. Diferencie os participantes.')
    if df['ID'].astype(str).duplicated().any():
        raise ValueError('IDs duplicados detectados.')
    if not df['Status de Pagamento'].isin(STATUS).all():
        raise ValueError('Status de pagamento inválido.')
    for value in df['Valor a Pagar (R$)']:
        cents(value)


def persist(ws, df):
    validate(df)
    values = [HEADERS]
    for _, row in df.iterrows():
        amount = cents(row['Valor a Pagar (R$)'])
        values.append([
            str(row['ID']), str(row['Nome']).strip(),
            f'{amount // 100}.{amount % 100:02d}',
            str(row['Status de Pagamento']), str(row['Observações']),
        ])
    # Atualização integral da tabela em uma única chamada; sem transações concorrentes.
    ws.update(range_name=f'A1:E{len(values)}', values=values, raw=True)
    # Limpa registros remanescentes quando houve exclusões.
    if ws.row_count > len(values):
        ws.batch_clear([f'A{len(values)+1}:E{ws.row_count}'])


st.title('🎉 Confraternização dos Amigos')
st.caption('Cadastro, pagamentos e lista de convidados sincronizados com Google Planilhas.')

try:
    worksheet = get_worksheet()
    df = load_data(worksheet)
    validate(df)
except Exception as exc:
    st.error(f'Não foi possível acessar os dados: {exc}')
    st.info('Confira os Secrets, o compartilhamento da planilha e o cabeçalho da aba.')
    st.stop()

expected = sum(cents(v) for v in df['Valor a Pagar (R$)'])
paid_df = df[df['Status de Pagamento'] == 'Pago']
collected = sum(cents(v) for v in paid_df['Valor a Pagar (R$)'])
remaining = expected - collected

m1, m2, m3, m4 = st.columns(4)
m1.metric('👥 Participantes', len(df))
m2.metric('💰 Arrecadado', brl(collected))
m3.metric('🎯 Esperado', brl(expected))
m4.metric('⏳ Pendentes', len(df) - len(paid_df))
st.progress(collected / expected if expected else 0, text=f'{len(paid_df)} pagos • {brl(remaining)} ainda pendentes')

cadastro, gestao, resumo = st.tabs(['➕ Cadastro', '✏️ Gerenciamento', '📋 Lista de convidados'])

with cadastro:
    st.subheader('Adicionar participante')
    with st.form('novo_participante', clear_on_submit=True):
        nome = st.text_input('Nome do participante', max_chars=120)
        valor = st.number_input('Valor a pagar (R$)', min_value=0.0, value=100.0, step=10.0, format='%.2f')
        status = st.selectbox('Status de pagamento', STATUS)
        observacoes = st.text_area('Observações', placeholder='Ex.: Vegetariano, chega mais tarde...')
        enviar = st.form_submit_button('Cadastrar participante', type='primary')
    if enviar:
        nome = nome.strip()
        if not nome:
            st.warning('Informe o nome.')
        elif nome.casefold() in df['Nome'].astype(str).str.casefold().tolist():
            st.warning('Já existe um participante com esse nome.')
        else:
            new = pd.DataFrame([{
                'ID': uuid.uuid4().hex,
                'Nome': nome,
                'Valor a Pagar (R$)': cents(valor) / 100,
                'Status de Pagamento': status,
                'Observações': observacoes,
            }])
            try:
                persist(worksheet, pd.concat([df, new], ignore_index=True))
                st.success('Participante cadastrado com sucesso!')
                st.rerun()
            except Exception as exc:
                st.error(f'Erro ao cadastrar: {exc}')

with gestao:
    st.subheader('Editar participantes e pagamentos')
    st.caption('Altere os dados e clique em Salvar alterações. O ID não pode ser editado.')
    edited = st.data_editor(
        df, key='editor', hide_index=True, use_container_width=True,
        num_rows='fixed',
        column_config={
            'ID': None,
            'Nome': st.column_config.TextColumn('Nome', required=True, max_chars=120),
            'Valor a Pagar (R$)': st.column_config.NumberColumn('Valor a Pagar (R$)', min_value=0, step=0.01, format='R$ %.2f', required=True),
            'Status de Pagamento': st.column_config.SelectboxColumn('Status', options=STATUS, required=True),
            'Observações': st.column_config.TextColumn('Observações', width='large'),
        },
    )
    if st.button('💾 Salvar alterações', type='primary'):
        try:
            edited['ID'] = df['ID'].values
            persist(worksheet, edited)
            st.success('Alterações salvas no Google Planilhas!')
            st.rerun()
        except Exception as exc:
            st.error(f'Não foi possível salvar: {exc}')

    st.divider()
    with st.expander('🗑️ Excluir participante'):
        if df.empty:
            st.info('Não há participantes cadastrados.')
        else:
            selected_id = st.selectbox(
                'Selecione quem deseja excluir', df['ID'].tolist(),
                format_func=lambda x: df.loc[df['ID'] == x, 'Nome'].iloc[0],
            )
            confirmed = st.checkbox('Confirmo que desejo excluir este participante.')
            if st.button('Excluir definitivamente', disabled=not confirmed):
                try:
                    persist(worksheet, df[df['ID'] != selected_id].reset_index(drop=True))
                    st.success('Participante excluído.')
                    st.rerun()
                except Exception as exc:
                    st.error(f'Erro ao excluir: {exc}')

with resumo:
    st.subheader('Lista de convidados')
    status_filter = st.selectbox('Filtrar por pagamento', ['Todos', *STATUS])
    query = st.text_input('Buscar por nome', placeholder='Digite parte do nome...')
    filtered = df.copy()
    if status_filter != 'Todos':
        filtered = filtered[filtered['Status de Pagamento'] == status_filter]
    if query.strip():
        filtered = filtered[filtered['Nome'].str.contains(query.strip(), case=False, regex=False, na=False)]
    filtered = filtered.sort_values('Nome', key=lambda s: s.str.casefold())
    st.dataframe(
        filtered.drop(columns=['ID']), hide_index=True, use_container_width=True,
        column_config={'Valor a Pagar (R$)': st.column_config.NumberColumn(format='R$ %.2f')},
    )
    st.caption(f'{len(filtered)} participante(s) na lista filtrada.')
    st.download_button('⬇️ Exportar lista CSV', filtered.drop(columns=['ID']).to_csv(index=False, sep=';', decimal=',').encode('utf-8-sig'),
                       file_name='convidados.csv', mime='text/csv')

st.divider()
if st.button('🔄 Atualizar dados do Google Planilhas'):
    st.rerun()
st.caption('Os valores arrecadados consideram o valor integral dos participantes marcados como Pago.')
