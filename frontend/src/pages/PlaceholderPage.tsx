interface PlaceholderPageProps {
  title: string
  description: string
}

export function PlaceholderPage({ title, description }: PlaceholderPageProps) {
  return (
    <div>
      <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
      <p className="mt-2 max-w-2xl text-slate-600">{description}</p>
      <p className="mt-6 inline-block rounded-md bg-slate-100 px-3 py-2 text-sm text-slate-700">
        Not available yet.
      </p>
    </div>
  )
}
