export function heavyFunction() {
  const arr: number[] = [];
  for (let i = 0; i < 10_000_000; i++) {
    arr.push(i);
  }
  return arr;
}

