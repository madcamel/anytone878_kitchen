// Remove entry points and labels at addresses with no memory block.
// The ARM Cortex pspec plants vector-table symbols (Reset, NMI, ..., IRQ) as
// entry points at 0x00000004-0x00000040, but this image loads at 0x08004000.
//@category Ham
import java.util.ArrayList;
import java.util.List;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolTable;

public class RemoveBogusVectorSyms extends GhidraScript {
	@Override
	public void run() throws Exception {
		Memory mem = currentProgram.getMemory();
		SymbolTable st = currentProgram.getSymbolTable();

		List<Address> eps = new ArrayList<>();
		st.getExternalEntryPointIterator().forEachRemaining(eps::add);
		int nEp = 0;
		for (Address a : eps) {
			if (!mem.contains(a)) {
				st.removeExternalEntryPoint(a);
				nEp++;
			}
		}

		List<Symbol> bad = new ArrayList<>();
		for (Symbol s : st.getAllSymbols(false)) {
			Address a = s.getAddress();
			if (a.isMemoryAddress() && !mem.contains(a)) {
				bad.add(s);
			}
		}
		int nSym = 0;
		for (Symbol s : bad) {
			println("deleting " + s.getName() + " @ " + s.getAddress());
			if (s.delete()) {
				nSym++;
			}
		}
		println("removed " + nEp + " entry points, " + nSym + " symbols");
	}
}
