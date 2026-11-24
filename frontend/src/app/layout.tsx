import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata={title:"Обзор качества · Биннофарм Групп",description:"Годовой обзор качества лекарственных препаратов"};
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="ru"><body>{children}</body></html>}
