import asyncio, pathlib
from playwright.async_api import async_playwright
here = pathlib.Path(__file__).parent.resolve()
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel='chrome', args=['--use-gl=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
        pg = await b.new_page(viewport={'width':1280,'height':769})
        for name in ('cover-jarvis','cover-telegram'):
            await pg.goto((here/'src'/f'{name}.html').as_uri())
            await pg.evaluate('document.fonts.ready'); await pg.wait_for_timeout(1200)
            await pg.screenshot(path=str(here/f'{name}.png'))
        # screenshots of the live site: hero and the terminal demo
        await pg.goto('http://localhost:4173/'); await pg.evaluate("localStorage.setItem('lang','en')"); await pg.reload()
        await pg.evaluate('document.fonts.ready'); await pg.wait_for_timeout(4500)
        await pg.screenshot(path=str(here/'site-hero.png'))
        await pg.evaluate("document.documentElement.style.scrollBehavior='auto'; scrollTo(0, document.querySelector('#demo').offsetTop - 30)")
        await pg.wait_for_timeout(9000)
        await pg.screenshot(path=str(here/'site-demo.png'))
        await b.close()
asyncio.run(main())
