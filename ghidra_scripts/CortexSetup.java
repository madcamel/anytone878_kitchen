// Pre-analysis setup for the raw GD32F303 (Cortex-M4) app image loaded at 0x08004000:
// RAM/peripheral/system blocks, vector table typed as dwords, every valid handler disassembled
// (Thumb) and made a function, Reset_Handler as entry point, and the stray vector symbols the
// Cortex pspec plants at 0x0-0x40 (nothing is mapped there) removed.
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.mem.*;
import ghidra.program.model.symbol.*;
import java.math.BigInteger;
import java.util.*;

public class CortexSetup extends GhidraScript {
	@Override
	public void run() throws Exception {
		Memory mem = currentProgram.getMemory();
		Object[][] blocks = {{"RAM", 0x20000000L, 0x18000L}, {"PERIPH", 0x40000000L, 0x100000L},
			{"SCS", 0xE0000000L, 0x100000L}};
		for (Object[] b : blocks) {
			Address a = toAddr((Long) b[1]);
			if (mem.getBlock(a) != null) continue;
			MemoryBlock mb = mem.createUninitializedBlock((String) b[0], a, (Long) b[2], false);
			mb.setRead(true); mb.setWrite(true); mb.setExecute(b[0].equals("RAM"));
			if (!b[0].equals("RAM")) mb.setVolatile(true);
		}
		Address base = mem.getMinAddress();
		long end = mem.getBlock(base).getEnd().getOffset();
		String[] names = {"_sp", "Reset", "NMI", "HardFault", "MemManage", "BusFault", "UsageFault", null,
			null, null, null, "SVC", "DebugMon", null, "PendSV", "SysTick"};
		Register tmode = currentProgram.getRegister("TMode");
		Set<Long> seen = new HashSet<>();
		for (int i = 0; i < 16 + 68; i++) {
			Address a = base.add(i * 4);
			long v = mem.getInt(a) & 0xffffffffL;
			createDWord(a);
			if (i == 0 || v < base.getOffset() || v > end || (v & 1) == 0) continue;
			long t = v & ~1L;
			if (!seen.add(t)) continue;
			String n = i == 1 ? "Reset_Handler" : (i < 16 && names[i] != null ? names[i] : "IRQ" + (i - 16) + "_Handler");
			Address fa = toAddr(t);
			currentProgram.getProgramContext().setValue(tmode, fa, fa, BigInteger.ONE);
			disassemble(fa);
			createFunction(fa, n);
			if (i == 1) addEntryPoint(fa);
		}
		println("vectors processed, handlers: " + seen.size());
		SymbolTable st = currentProgram.getSymbolTable();
		List<Address> eps = new ArrayList<>();
		st.getExternalEntryPointIterator().forEachRemaining(eps::add);
		for (Address ep : eps) if (!mem.contains(ep)) st.removeExternalEntryPoint(ep);
		List<Symbol> bad = new ArrayList<>();
		for (Symbol s : st.getAllSymbols(false))
			if (s.getAddress().isMemoryAddress() && !mem.contains(s.getAddress())) bad.add(s);
		for (Symbol s : bad) s.delete();
	}
}
