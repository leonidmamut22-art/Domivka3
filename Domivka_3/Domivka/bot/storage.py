"""Bounded SQLite/PostgreSQL storage compatibility for Domivka.

SQLite remains the local default. DATABASE_URL selects PostgreSQL. The SQL
adapter supports this application's dialect, not arbitrary SQLite programs.
Keep application values bound to placeholders; no values are interpolated.
PostgreSQL uses UTC text dates to preserve the existing domain/API contract.
"""
import asyncio
import datetime as dt
import decimal
import os
import re
import sqlite3
from pathlib import Path

TOKEN=re.compile(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|\s+|.",re.S)
IDENT=re.compile(r'^[A-Za-z_][A-Za-z_0-9]*$')
SCHEMA_EPOCH=0
SCHEMA_IDENT=re.compile(r'^[a-z][a-z0-9_]{0,62}$')


def is_postgres(target=None):
    target=os.environ.get('DATABASE_URL','') if target is None else str(target)
    return target.startswith(('postgresql://','postgres://'))


def target(local_path):
    configured=os.environ.get('DATABASE_URL','').strip()
    if configured and not is_postgres(configured):
        raise ValueError('DATABASE_URL must be a PostgreSQL URL; omit it for local SQLite')
    return configured or str(local_path)


def schema_name():
    schema=os.environ.get('DATABASE_SCHEMA','public')
    if not SCHEMA_IDENT.fullmatch(schema):raise ValueError('Invalid DATABASE_SCHEMA identifier')
    return schema


def quote_identifier(value):
    if not IDENT.fullmatch(value):raise ValueError('Invalid SQL identifier')
    return '"'+value+'"'


def _string(token):
    return token[1:-1].replace("''", "'") if token.startswith("'") else None


def _function(tokens,i):
    j=i+1
    while j<len(tokens) and tokens[j].isspace():j+=1
    if j>=len(tokens) or tokens[j]!='(':return None
    depth=1;k=j+1;args=[];start=k
    while k<len(tokens):
        if tokens[k]=='(':depth+=1
        elif tokens[k]==')':
            depth-=1
            if not depth:
                args.append(''.join(tokens[start:k]).strip())
                return k,args
        elif tokens[k]==',' and depth==1:args.append(''.join(tokens[start:k]).strip());start=k+1
        k+=1
    raise ValueError('Unclosed SQL function')


def translate(sql,bind=False):
    """Translate only SQL syntax, never quoted literals or bound values."""
    tokens=TOKEN.findall(sql)
    out=[];i=0;ddl=bool(re.match(r'\s*(create|alter)\b',sql,re.I))
    while i<len(tokens):
        token=tokens[i];lower=token.lower()
        if lower in ('datetime','date'):
            parsed=_function(tokens,i)
            if parsed:
                end,args=parsed
                if args and args[0].lower()=="'now'":
                    expr="(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')"
                    for modifier in args[1:]:
                        literal=_string(modifier)
                        if not literal or not re.fullmatch(r'[+-]?\d+\s+(?:day|days|hour|hours|minute|minutes|second|seconds)',literal,re.I):
                            raise ValueError('Unsupported SQLite time modifier')
                        expr="("+expr+" + INTERVAL '"+literal+"')"
                    fmt='YYYY-MM-DD HH24:MI:SS' if lower=='datetime' else 'YYYY-MM-DD'
                    out.append("to_char("+expr+", '"+fmt+"')");i=end+1;continue
                if lower=='date' and len(args)==1:
                    out.append('substring('+translate(args[0],bind)+',1,10)');i=end+1;continue
                raise ValueError('Unsupported SQLite date function')
        if lower=='like':out.append('ILIKE')
        elif lower=='user':out.append('"user"')
        elif token=='?':out.append('%s')
        elif ddl and lower=='integer':out.append('bigint')
        elif ddl and lower=='real':out.append('double precision')
        elif lower=='rowid':out.append('listing_id')  # cmp ordering has no implicit PG rowid
        else:out.append(token.replace('%','%%') if bind else token)
        i+=1
    result=''.join(out).strip().rstrip(';')
    if ddl:
        result=re.sub(r'\bbigint\s+primary\s+key\s+autoincrement\b','bigserial primary key',result,flags=re.I)
    # SQLite accepts this non-standard GROUP BY by following a join dependency.
    result=re.sub(r'group\s+by\s+r\.ref_by\s+order\s+by','group by r.ref_by,u.name order by',result,flags=re.I)
    return result


def statements(script):
    parts=[];current=[]
    for token in TOKEN.findall(script):
        if token==';':
            statement=''.join(current).strip()
            if statement:parts.append(statement)
            current=[]
        else:current.append(token)
    statement=''.join(current).strip()
    if statement:parts.append(statement)
    return parts


class Row(dict):
    def __getitem__(self,key):
        if isinstance(key,int):return tuple(self.values())[key]
        return super().__getitem__(key)


def _value(value):
    if isinstance(value,decimal.Decimal):return int(value) if value==value.to_integral_value() else float(value)
    if isinstance(value,(dt.datetime,dt.date)):return value.isoformat(sep=' ') if isinstance(value,dt.datetime) else value.isoformat()
    return value


class Cursor:
    def __init__(self,rows=(),rowcount=0,lastrowid=None):
        self.rows=[Row((k,_value(v)) for k,v in row.items()) for row in rows]
        self.rowcount=rowcount;self.lastrowid=lastrowid;self.index=0
    def fetchone(self):
        if self.index>=len(self.rows):return None
        row=self.rows[self.index];self.index+=1;return row
    def fetchall(self):
        rows=self.rows[self.index:];self.index=len(self.rows);return rows
    def __iter__(self):return iter(self.fetchall())
    def close(self):pass


class PostgresConnection:
    backend='postgresql'
    def __init__(self,url):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:raise RuntimeError('Install psycopg[binary] for PostgreSQL storage') from None
        self.psycopg=psycopg;self.schema=schema_name();self.row_factory=None;self.tables={};self.epoch=SCHEMA_EPOCH
        self.raw=psycopg.connect(url,autocommit=True,row_factory=dict_row,connect_timeout=15,application_name='domivka')
        if self.schema!='public':self.raw.execute('CREATE SCHEMA IF NOT EXISTS '+quote_identifier(self.schema))
        self.raw.execute('SET search_path TO '+quote_identifier(self.schema))
        self.raw.execute("SET TIME ZONE 'UTC'")
        self.raw.execute("SET statement_timeout TO '30s'")
        self.raw.execute("SET lock_timeout TO '5s'")
        self.raw.execute("SET idle_in_transaction_session_timeout TO '60s'")
    @property
    def in_transaction(self):
        return self.raw.info.transaction_status==self.psycopg.pq.TransactionStatus.INTRANS
    def _columns(self,table):
        if self.epoch!=SCHEMA_EPOCH:self.tables.clear();self.epoch=SCHEMA_EPOCH
        if table not in self.tables:
            rows=self.raw.execute('select column_name name,data_type type,column_default dflt_value,is_nullable from information_schema.columns where table_schema=%s and table_name=%s order by ordinal_position',(self.schema,table)).fetchall()
            pks=self.raw.execute("select a.attname name from pg_index i join pg_class t on t.oid=i.indrelid join pg_namespace n on n.oid=t.relnamespace join pg_attribute a on a.attrelid=t.oid and a.attnum=any(i.indkey) where i.indisprimary and n.nspname=%s and t.relname=%s order by a.attnum",(self.schema,table)).fetchall()
            pk={r['name']:i+1 for i,r in enumerate(pks)}
            self.tables[table]=[{'cid':i,'name':r['name'],'type':r['type'],'notnull':int(r['is_nullable']=='NO'),'dflt_value':r['dflt_value'],'pk':pk.get(r['name'],0)} for i,r in enumerate(rows)]
        return self.tables[table]
    def execute(self,sql,args=()):
        original=sql.strip().rstrip(';')
        pragma=re.fullmatch(r'pragma\s+table_info\(([A-Za-z_][A-Za-z_0-9]*)\)',original,re.I)
        if pragma:
            self.tables.pop(pragma.group(1),None)
            return Cursor(self._columns(pragma.group(1)))
        if re.fullmatch(r'pragma\s+database_list',original,re.I):return Cursor([{'seq':0,'name':'main','file':'postgresql'}])
        if re.fullmatch(r'pragma\s+journal_mode\s*=\s*wal',original,re.I):return Cursor([{'journal_mode':'postgresql'}])
        if original.lower().startswith('pragma'):raise ValueError('Unsupported PRAGMA in PostgreSQL mode')
        if re.fullmatch(r'begin\s+immediate',original,re.I):
            self.raw.execute('BEGIN')
            try:self.raw.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':domivka-write',0))")
            except Exception:
                self.raw.rollback();raise
            return Cursor()
        if re.search(r'\bsqlite_master\b',original,re.I):
            if not re.fullmatch(r"select\s+1\s+from\s+sqlite_master\s+where\s+type\s*=\s*'table'\s+and\s+name\s*=\s*\?",original,re.I):
                raise ValueError('Unsupported SQLite catalog query')
            original='select 1 from information_schema.tables where table_schema=current_schema() and table_name=?'
        ignored=bool(re.match(r'insert\s+or\s+ignore\s+',original,re.I))
        replaced=bool(re.match(r'insert\s+or\s+replace\s+',original,re.I))
        if ignored or replaced:original=re.sub(r'^(insert)\s+or\s+(ignore|replace)',r'\1',original,flags=re.I)
        translated=translate(original,bool(args))
        insert=re.match(r'insert\s+into\s+([A-Za-z_][A-Za-z_0-9]*)(?:\s*\(([^)]+)\))?',original,re.I)
        returning=False
        if insert:
            table=insert.group(1);columns=self._columns(table)
            if ignored:translated+=' ON CONFLICT DO NOTHING'
            if replaced:
                keys=[quote_identifier(c['name']) for c in columns if c['pk']]
                supplied=[c.strip().strip('"') for c in insert.group(2).split(',')] if insert.group(2) else [c['name'] for c in columns]
                updates=[quote_identifier(c)+'=excluded.'+quote_identifier(c) for c in supplied if quote_identifier(c) not in keys]
                if not keys:raise ValueError('REPLACE requires a primary key')
                translated+=' ON CONFLICT ('+','.join(keys)+') DO '+('UPDATE SET '+','.join(updates) if updates else 'NOTHING')
            if any(c['name']=='id' for c in columns) and not re.search(r'\breturning\b',translated,re.I):
                translated+=' RETURNING id';returning=True
        savepoint=self.in_transaction and not re.match(r'\s*(begin|commit|rollback|savepoint|release)\b',translated,re.I)
        if savepoint:self.raw.execute('SAVEPOINT domivka_statement')
        try:
            # psycopg's parameter protocol reserves percent signs, including in
            # SQL string literals. Escape them before inserting placeholders.
            cur=self.raw.execute(translated,tuple(int(v) if isinstance(v,bool) else v for v in args) if args else None)
            rows=cur.fetchall() if cur.description else []
            count=cur.rowcount
            last=rows[-1].get('id') if returning and rows else None
            if savepoint:self.raw.execute('RELEASE SAVEPOINT domivka_statement')
            if re.match(r'\s*(create|alter|drop)\b',translated,re.I):
                global SCHEMA_EPOCH
                SCHEMA_EPOCH+=1;self.tables.clear();self.epoch=SCHEMA_EPOCH
            return Cursor(rows,count,last)
        except Exception as e:
            if savepoint:
                self.raw.execute('ROLLBACK TO SAVEPOINT domivka_statement')
                self.raw.execute('RELEASE SAVEPOINT domivka_statement')
            if isinstance(e,self.psycopg.IntegrityError):raise sqlite3.IntegrityError('Database constraint violated') from e
            raise
    def executescript(self,script):
        for statement in statements(script):self.execute(statement)
        return Cursor()
    def commit(self):self.raw.commit()
    def rollback(self):self.raw.rollback()
    def close(self):self.raw.close()


def connect(database,timeout=3):
    if is_postgres(database):return PostgresConnection(database)
    conn=sqlite3.connect(str(database),timeout=timeout)
    conn.row_factory=sqlite3.Row
    return conn


class AsyncCursor:
    def __init__(self,cursor):self.cursor=cursor;self.lastrowid=cursor.lastrowid;self.rowcount=cursor.rowcount
    async def fetchall(self):return self.cursor.fetchall()
    async def fetchone(self):return self.cursor.fetchone()
    async def close(self):self.cursor.close()


class AsyncPostgresConnection:
    """Matches the small aiosqlite surface used by the existing rental engine.

    Database calls are synchronous on the event-loop thread, matching the
    current domain modules. This preserves non-interleaving legacy transactions
    but limits throughput; statement/lock/connect timeouts bound network waits.
    Run one bot process per deployment. A future async repository refactor can
    offload entire domain transactions, not individual statements.
    """
    backend='postgresql'
    def __init__(self,database):self.connection=connect(database);self.row_factory=None
    async def execute(self,sql,args=()):return AsyncCursor(self.connection.execute(sql,args))
    async def executescript(self,script):return AsyncCursor(self.connection.executescript(script))
    async def commit(self):self.connection.commit()
    async def rollback(self):self.connection.rollback()
    async def close(self):self.connection.close()
    async def backup(self,_):raise RuntimeError('Use PostgreSQL provider backup or pg_dump, not SQLite backup')


async def connect_async(database):
    if is_postgres(database):return AsyncPostgresConnection(database)
    import aiosqlite
    conn=await aiosqlite.connect(str(database));conn.row_factory=aiosqlite.Row
    return conn
