alter table public.orders
  add column if not exists quantity integer,
  add column if not exists price_krw integer;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.orders'::regclass
      and conname = 'orders_quantity_positive'
  ) then
    alter table public.orders
      add constraint orders_quantity_positive
      check (quantity is null or quantity > 0);
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conrelid = 'public.orders'::regclass
      and conname = 'orders_price_krw_positive'
  ) then
    alter table public.orders
      add constraint orders_price_krw_positive
      check (price_krw is null or price_krw > 0);
  end if;
end
$$;
